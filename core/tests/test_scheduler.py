"""Tests for the intelligent scheduler.

Covers:
- _is_sat_peak boundary detection
- calcular_proximo_scrape for all 4 frequencies
- _build_download_params cross-year logic
- _init_missing_proximo_scrape auto-seeding
- programar_descargas_del_dia integration (rate limiting, jitter, enqueue)
"""

from datetime import datetime, time, timedelta, timezone
from unittest.mock import patch, MagicMock

from django.contrib.auth.models import User
from django.test import TestCase

from core.models import Empresa, ScheduleConfig
from core.services.scheduler import (
    _is_sat_peak,
    _build_download_params,
    _init_missing_proximo_scrape,
    calcular_proximo_scrape,
    SAT_PEAK_START_UTC,
    SAT_PEAK_END_UTC,
    RATE_LIMIT_SECONDS,
)


class SATpeakTests(TestCase):
    """Test _is_sat_peak boundary detection."""

    def test_before_peak(self):
        """16:59 UTC is before peak."""
        dt = datetime(2026, 3, 15, 16, 59, tzinfo=timezone.utc)
        self.assertFalse(_is_sat_peak(dt))

    def test_at_peak_start(self):
        """17:00 UTC is peak start."""
        dt = datetime(2026, 3, 15, 17, 0, tzinfo=timezone.utc)
        self.assertTrue(_is_sat_peak(dt))

    def test_mid_peak(self):
        """19:00 UTC is mid-peak."""
        dt = datetime(2026, 3, 15, 19, 0, tzinfo=timezone.utc)
        self.assertTrue(_is_sat_peak(dt))

    def test_at_peak_end(self):
        """21:00 UTC is peak end."""
        dt = datetime(2026, 3, 15, 21, 0, tzinfo=timezone.utc)
        self.assertTrue(_is_sat_peak(dt))

    def test_after_peak(self):
        """21:01 UTC is after peak."""
        dt = datetime(2026, 3, 15, 21, 1, tzinfo=timezone.utc)
        self.assertFalse(_is_sat_peak(dt))

    def test_morning(self):
        """8:00 UTC is not peak."""
        dt = datetime(2026, 3, 15, 8, 0, tzinfo=timezone.utc)
        self.assertFalse(_is_sat_peak(dt))


class CalcularProximoScrapeTests(TestCase):
    """Test calcular_proximo_scrape for all frequencies."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user("test", password="test")
        cls.empresa = Empresa.objects.create(
            nombre="Test Corp", rfc="TEST010101AAA", owner=cls.user,
        )

    def _make_schedule(self, **kwargs):
        defaults = {
            "empresa": self.empresa,
            "frecuencia": "semanal",
            "hora_preferida": time(6, 0),  # 6 AM UTC
            "jitter_minutos": 0,  # Disable jitter for deterministic tests
        }
        defaults.update(kwargs)
        schedule, _ = ScheduleConfig.objects.update_or_create(
            empresa=self.empresa, defaults=defaults,
        )
        return schedule

    def test_diaria(self):
        """Diaria should be tomorrow at hora_preferida."""
        schedule = self._make_schedule(frecuencia="diaria", hora_preferida=time(6, 0))
        now = datetime(2026, 3, 15, 10, 0, tzinfo=timezone.utc)
        result = calcular_proximo_scrape(schedule, now)

        self.assertEqual(result.date(), now.date() + timedelta(days=1))
        self.assertEqual(result.hour, 6)

    def test_semanal_monday(self):
        """Semanal with dia_semana=0 (Monday). 2026-03-15 is Sunday."""
        schedule = self._make_schedule(
            frecuencia="semanal", dia_semana=0, hora_preferida=time(6, 0),
        )
        now = datetime(2026, 3, 15, 10, 0, tzinfo=timezone.utc)  # Sunday
        result = calcular_proximo_scrape(schedule, now)

        # Next Monday is March 16
        self.assertEqual(result.date().weekday(), 0)  # Monday
        self.assertEqual(result.date(), now.date() + timedelta(days=1))

    def test_semanal_same_weekday(self):
        """If dia_semana == today, should jump to next week."""
        schedule = self._make_schedule(
            frecuencia="semanal", dia_semana=6, hora_preferida=time(6, 0),
        )
        now = datetime(2026, 3, 15, 10, 0, tzinfo=timezone.utc)  # Sunday = 6
        result = calcular_proximo_scrape(schedule, now)

        # Should be next Sunday (7 days ahead)
        self.assertEqual(result.date(), now.date() + timedelta(days=7))

    def test_quincenal(self):
        """Quincenal should be 15 days ahead."""
        schedule = self._make_schedule(frecuencia="quincenal", hora_preferida=time(6, 0))
        now = datetime(2026, 3, 15, 10, 0, tzinfo=timezone.utc)
        result = calcular_proximo_scrape(schedule, now)

        self.assertEqual(result.date(), now.date() + timedelta(days=15))

    def test_mensual(self):
        """Mensual should be same day next month."""
        schedule = self._make_schedule(frecuencia="mensual", hora_preferida=time(6, 0))
        now = datetime(2026, 3, 15, 10, 0, tzinfo=timezone.utc)
        result = calcular_proximo_scrape(schedule, now)

        self.assertEqual(result.month, 4)
        self.assertEqual(result.day, 15)

    def test_mensual_year_rollover(self):
        """Mensual in December should roll to January next year."""
        schedule = self._make_schedule(frecuencia="mensual", hora_preferida=time(6, 0))
        now = datetime(2026, 12, 15, 10, 0, tzinfo=timezone.utc)
        result = calcular_proximo_scrape(schedule, now)

        self.assertEqual(result.year, 2027)
        self.assertEqual(result.month, 1)
        self.assertEqual(result.day, 15)

    def test_mensual_short_month(self):
        """Mensual Jan 31 → Feb should clamp to 28."""
        schedule = self._make_schedule(frecuencia="mensual", hora_preferida=time(6, 0))
        now = datetime(2026, 1, 31, 10, 0, tzinfo=timezone.utc)
        result = calcular_proximo_scrape(schedule, now)

        self.assertEqual(result.month, 2)
        self.assertEqual(result.day, 28)

    def test_peak_avoidance(self):
        """If hora_preferida falls in SAT peak, it should shift to after 21:00 UTC."""
        schedule = self._make_schedule(
            frecuencia="diaria", hora_preferida=time(18, 0), jitter_minutos=0,
        )
        now = datetime(2026, 3, 15, 10, 0, tzinfo=timezone.utc)
        result = calcular_proximo_scrape(schedule, now)

        # 18:00 is in peak (17-21), should shift to 21:xx
        self.assertEqual(result.hour, 21)
        self.assertGreaterEqual(result.minute, 5)

    def test_jitter_adds_minutes(self):
        """Jitter should add randomness within configured range."""
        schedule = self._make_schedule(
            frecuencia="diaria", hora_preferida=time(6, 0), jitter_minutos=30,
        )
        now = datetime(2026, 3, 15, 10, 0, tzinfo=timezone.utc)

        # Run multiple times to verify jitter is applied
        results = set()
        for _ in range(20):
            result = calcular_proximo_scrape(schedule, now)
            results.add(result.minute)

        # With 30 min jitter, we should get some variation
        # (statistically very very unlikely to get all same minute in 20 runs)
        self.assertGreater(len(results), 1)


class BuildDownloadParamsTests(TestCase):
    """Test _build_download_params cross-year logic."""

    def _make_schedule(self, meses_atras=1):
        mock = MagicMock()
        mock.meses_atras = meses_atras
        return mock

    def test_single_month_same_year(self):
        """meses_atras=1 in March → March only."""
        schedule = self._make_schedule(meses_atras=1)
        now = datetime(2026, 3, 15, 10, 0, tzinfo=timezone.utc)
        result = _build_download_params(schedule, now)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["year"], 2026)
        self.assertEqual(result[0]["month_start"], 3)
        self.assertEqual(result[0]["month_end"], 3)

    def test_two_months_same_year(self):
        """meses_atras=2 in March → Feb-Mar."""
        schedule = self._make_schedule(meses_atras=2)
        now = datetime(2026, 3, 15, 10, 0, tzinfo=timezone.utc)
        result = _build_download_params(schedule, now)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["month_start"], 2)
        self.assertEqual(result[0]["month_end"], 3)

    def test_cross_year_jan_with_2_months(self):
        """meses_atras=2 in January → Dec prev year + Jan current year."""
        schedule = self._make_schedule(meses_atras=2)
        now = datetime(2026, 1, 15, 10, 0, tzinfo=timezone.utc)
        result = _build_download_params(schedule, now)

        self.assertEqual(len(result), 2)
        # Previous year: December
        self.assertEqual(result[0]["year"], 2025)
        self.assertEqual(result[0]["month_start"], 12)
        self.assertEqual(result[0]["month_end"], 12)
        # Current year: January
        self.assertEqual(result[1]["year"], 2026)
        self.assertEqual(result[1]["month_start"], 1)
        self.assertEqual(result[1]["month_end"], 1)

    def test_cross_year_jan_with_3_months(self):
        """meses_atras=3 in January → Nov-Dec prev year + Jan current year."""
        schedule = self._make_schedule(meses_atras=3)
        now = datetime(2026, 1, 15, 10, 0, tzinfo=timezone.utc)
        result = _build_download_params(schedule, now)

        self.assertEqual(len(result), 2)
        # Previous year: Nov-Dec
        self.assertEqual(result[0]["year"], 2025)
        self.assertEqual(result[0]["month_start"], 11)
        self.assertEqual(result[0]["month_end"], 12)
        # Current year: January
        self.assertEqual(result[1]["year"], 2026)
        self.assertEqual(result[1]["month_start"], 1)
        self.assertEqual(result[1]["month_end"], 1)

    def test_cross_year_feb_with_3_months(self):
        """meses_atras=3 in February → Dec prev year + Jan-Feb current year."""
        schedule = self._make_schedule(meses_atras=3)
        now = datetime(2026, 2, 15, 10, 0, tzinfo=timezone.utc)
        result = _build_download_params(schedule, now)

        self.assertEqual(len(result), 2)
        # Previous year: December
        self.assertEqual(result[0]["year"], 2025)
        self.assertEqual(result[0]["month_start"], 12)
        self.assertEqual(result[0]["month_end"], 12)
        # Current year: Jan-Feb
        self.assertEqual(result[1]["year"], 2026)
        self.assertEqual(result[1]["month_start"], 1)
        self.assertEqual(result[1]["month_end"], 2)

    def test_exact_boundary_meses_equals_month(self):
        """meses_atras=1 in January → just January (no cross-year)."""
        schedule = self._make_schedule(meses_atras=1)
        now = datetime(2026, 1, 15, 10, 0, tzinfo=timezone.utc)
        result = _build_download_params(schedule, now)

        # meses_atras=1 equals now.month=1, not greater → same year
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["year"], 2026)
        self.assertEqual(result[0]["month_start"], 1)
        self.assertEqual(result[0]["month_end"], 1)


class InitMissingProximoScrapeTests(TestCase):
    """Test _init_missing_proximo_scrape auto-seeding."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user("test2", password="test")

    def test_seeds_null_proximo(self):
        """Empresa with null proximo_scrape should get initialized."""
        empresa = Empresa.objects.create(
            nombre="New Corp", rfc="NEW010101AAA", owner=self.user,
            descarga_activa=True, proximo_scrape=None,
        )
        ScheduleConfig.objects.create(
            empresa=empresa, activo=True,
            frecuencia="semanal", hora_preferida=time(6, 0),
        )

        now = datetime(2026, 3, 15, 10, 0, tzinfo=timezone.utc)
        count = _init_missing_proximo_scrape(now)

        self.assertEqual(count, 1)
        empresa.refresh_from_db()
        self.assertIsNotNone(empresa.proximo_scrape)

    def test_skips_existing_proximo(self):
        """Empresa with existing proximo_scrape should not be touched."""
        existing = datetime(2026, 3, 20, 6, 0, tzinfo=timezone.utc)
        empresa = Empresa.objects.create(
            nombre="Existing Corp", rfc="EXI010101AAA", owner=self.user,
            descarga_activa=True, proximo_scrape=existing,
        )
        ScheduleConfig.objects.create(
            empresa=empresa, activo=True,
            frecuencia="semanal", hora_preferida=time(6, 0),
        )

        now = datetime(2026, 3, 15, 10, 0, tzinfo=timezone.utc)
        count = _init_missing_proximo_scrape(now)

        self.assertEqual(count, 0)
        empresa.refresh_from_db()
        self.assertEqual(empresa.proximo_scrape, existing)

    def test_skips_inactive_schedule(self):
        """Inactive ScheduleConfig should be skipped."""
        empresa = Empresa.objects.create(
            nombre="Inactive Corp", rfc="INA010101AAA", owner=self.user,
            descarga_activa=True, proximo_scrape=None,
        )
        ScheduleConfig.objects.create(
            empresa=empresa, activo=False,
            frecuencia="semanal", hora_preferida=time(6, 0),
        )

        now = datetime(2026, 3, 15, 10, 0, tzinfo=timezone.utc)
        count = _init_missing_proximo_scrape(now)

        self.assertEqual(count, 0)


class ProgramarDescargasIntegrationTests(TestCase):
    """Integration test for programar_descargas_del_dia."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user("test3", password="test")

    @patch("core.services.scheduler.random.randint", return_value=0)
    @patch("core.tasks.descargar_cfdis.apply_async")
    def test_enqueues_due_empresa(self, mock_apply, mock_randint):
        """Due empresa with FIEL should be enqueued."""
        from core.services.scheduler import programar_descargas_del_dia

        empresa = Empresa.objects.create(
            nombre="Due Corp", rfc="DUE010101AAA", owner=self.user,
            descarga_activa=True,
            proximo_scrape=datetime(2026, 3, 15, 9, 0, tzinfo=timezone.utc),
            fiel_cer_key="test.cer", fiel_key_key="test.key",
        )
        ScheduleConfig.objects.create(
            empresa=empresa, activo=True,
            frecuencia="semanal", hora_preferida=time(6, 0),
            jitter_minutos=0, meses_atras=1,
        )

        # Run at 10 AM UTC (not peak)
        with patch("core.services.scheduler.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 3, 15, 10, 0, tzinfo=timezone.utc)
            mock_dt.combine = datetime.combine
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            result = programar_descargas_del_dia()

        self.assertEqual(result["queued"], 1)
        mock_apply.assert_called_once()

        # Verify triggered_by is "schedule"
        call_kwargs = mock_apply.call_args[1]
        self.assertEqual(call_kwargs["kwargs"]["triggered_by"], "schedule")

    @patch("core.tasks.descargar_cfdis.apply_async")
    def test_skips_peak_hours(self, mock_apply):
        """Should skip during SAT peak hours."""
        from core.services.scheduler import programar_descargas_del_dia

        empresa = Empresa.objects.create(
            nombre="Peak Corp", rfc="PEA010101AAA", owner=self.user,
            descarga_activa=True,
            proximo_scrape=datetime(2026, 3, 15, 9, 0, tzinfo=timezone.utc),
            fiel_cer_key="test.cer", fiel_key_key="test.key",
        )
        ScheduleConfig.objects.create(
            empresa=empresa, activo=True,
            frecuencia="semanal", hora_preferida=time(6, 0),
        )

        # Run at 18:00 UTC (peak)
        with patch("core.services.scheduler.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 3, 15, 18, 0, tzinfo=timezone.utc)
            mock_dt.combine = datetime.combine
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            result = programar_descargas_del_dia()

        self.assertEqual(result["status"], "skipped")
        mock_apply.assert_not_called()

    @patch("core.services.scheduler.random.randint", return_value=0)
    @patch("core.tasks.descargar_cfdis.apply_async")
    def test_rate_limiting_between_rfcs(self, mock_apply, mock_randint):
        """Multiple empresas should have 5-min gaps between them."""
        from core.services.scheduler import programar_descargas_del_dia

        for i in range(3):
            emp = Empresa.objects.create(
                nombre=f"Rate Corp {i}", rfc=f"RAT0{i}0101AAA", owner=self.user,
                descarga_activa=True,
                proximo_scrape=datetime(2026, 3, 15, 9, 0, tzinfo=timezone.utc),
                fiel_cer_key="test.cer", fiel_key_key="test.key",
            )
            ScheduleConfig.objects.create(
                empresa=emp, activo=True,
                frecuencia="semanal", hora_preferida=time(6, 0),
                jitter_minutos=0, meses_atras=1,
            )

        with patch("core.services.scheduler.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 3, 15, 10, 0, tzinfo=timezone.utc)
            mock_dt.combine = datetime.combine
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            result = programar_descargas_del_dia()

        self.assertEqual(result["queued"], 3)
        self.assertEqual(mock_apply.call_count, 3)

        # Verify rate limiting: countdowns should be 0, 300, 600
        countdowns = [call[1]["countdown"] for call in mock_apply.call_args_list]
        self.assertEqual(countdowns, [0, RATE_LIMIT_SECONDS, RATE_LIMIT_SECONDS * 2])

    @patch("core.tasks.descargar_cfdis.apply_async")
    def test_skips_empresa_without_fiel(self, mock_apply):
        """Empresa without FIEL configured should be skipped."""
        from core.services.scheduler import programar_descargas_del_dia

        empresa = Empresa.objects.create(
            nombre="No FIEL Corp", rfc="NOF010101AAA", owner=self.user,
            descarga_activa=True,
            proximo_scrape=datetime(2026, 3, 15, 9, 0, tzinfo=timezone.utc),
            # No fiel_cer_key or fiel_key_key
        )
        ScheduleConfig.objects.create(
            empresa=empresa, activo=True,
            frecuencia="semanal", hora_preferida=time(6, 0),
        )

        with patch("core.services.scheduler.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 3, 15, 10, 0, tzinfo=timezone.utc)
            mock_dt.combine = datetime.combine
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            result = programar_descargas_del_dia()

        self.assertEqual(result["queued"], 0)
        mock_apply.assert_not_called()
