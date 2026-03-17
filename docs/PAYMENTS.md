# Cirrus — Módulo de Pagos (Stripe)

## Configuración
- **Stripe account**: Cirrus.Nubex.me
- **Modo**: Test (cambiar a Live cuando esté probado)
- **Webhook**: `https://cirrus.nubex.me/api/v1/stripe/webhook/`

## Productos en Stripe

| Producto | Precio | Tipo | Stripe Product ID |
|----------|--------|------|-------------------|
| Cirrus Básico | $199 MXN/mes | Suscripción | prod_UAMX... |
| Cirrus Profesional | $499 MXN/mes | Suscripción | prod_UAMX... |
| Cirrus Enterprise | $1,299 MXN/mes | Suscripción | prod_UAMX... |
| Año Histórico | $500 MXN | Pago único | prod_UAMX... |
| RFC Adicional | $49 MXN/mes | Suscripción | prod_UAMX... |
| Colaborador | $30 MXN/mes | Suscripción | prod_UAMX... |

## Métodos de pago
- Tarjeta de crédito/débito
- (OXXO y SPEI disponibles en modo Live)

## Flujo de suscripción
1. Cliente va a `/app/mejorar-plan/`
2. Elige plan → POST a `/app/checkout/`
3. Se crea Stripe Checkout Session
4. Redirige a Stripe → cliente paga
5. Stripe redirige a `/app/pago-exitoso/`
6. Webhook confirma pago → activa plan en BD

## Flujo de pago único (histórico)
1. Cliente solicita año histórico desde `/app/descargas/`
2. POST a `/app/comprar-historico/checkout/`
3. Redirige a Stripe Checkout ($500 MXN)
4. Webhook activa descarga del año histórico

## Webhook events procesados
- `checkout.session.completed` → activar plan / activar histórico
- `invoice.paid` → renovación exitosa
- `invoice.payment_failed` → marcar `past_due`
- `customer.subscription.deleted` → degradar a gratis

## Archivos
- `core/services/stripe_service.py` — Lógica de pagos
- `core/management/commands/setup_stripe.py` — Crear productos
- `accounts/views.py` — Views de pago (mejorar_plan, checkout, etc.)
- `cirrus/urls.py` — Webhook endpoint
- `accounts/urls.py` — Rutas de pago
- `frontend/templates/app/mejorar_plan.html` — Página de planes
- `frontend/templates/app/pago_exitoso.html` — Confirmación de pago

## Modelos
- `Plan.stripe_product_id` / `Plan.stripe_price_id`
- `ClienteProfile.stripe_customer_id` / `.stripe_subscription_id` / `.subscription_status`
- `StripePayment` — Registro de pagos

## Cambiar a Live
1. Cambiar keys a `pk_live_` y `sk_live_` en `.env`
2. `STRIPE_TEST_MODE=False`
3. Reconfigurar webhook con URL de producción
4. `python manage.py setup_stripe` (recrear productos en Live)
5. Reiniciar servicios

## Tarjeta de prueba
- Número: `4242 4242 4242 4242`
- Fecha: cualquier fecha futura
- CVC: cualquier 3 dígitos
