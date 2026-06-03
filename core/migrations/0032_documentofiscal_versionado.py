# Cerebro Fiscal — versionado de documentos.
# - Agrega metadata_extra (JSON) a DocumentoFiscal
# - Expande ESTADO_CHOICES con 'requiere_decision' y 'archivado'

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0031_rename_core_stripe_estado_idx_core_stripe_estado_e19a05_idx_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='documentofiscal',
            name='metadata_extra',
            field=models.JSONField(
                blank=True, default=dict,
                help_text='Datos auxiliares (ej. version_anterior_id para decisión admin)',
            ),
        ),
        migrations.AlterField(
            model_name='documentofiscal',
            name='estado',
            field=models.CharField(
                choices=[
                    ('recibido', 'Recibido'),
                    ('convirtiendo', 'Convirtiendo (Docling)'),
                    ('convertido', 'Convertido a Markdown'),
                    ('validando', 'Validando (Qwen)'),
                    ('rechazado', 'Rechazado — no fiscal'),
                    ('validado', 'Validado — metadata OK'),
                    ('requiere_decision', 'Versión anterior detectada — requiere acción'),
                    ('embeddiendo', 'Generando embeddings'),
                    ('indexado', 'Indexado'),
                    ('archivado', 'Archivado — reemplazado por nueva versión'),
                    ('error', 'Error'),
                ],
                db_index=True, default='recibido', max_length=20,
            ),
        ),
    ]
