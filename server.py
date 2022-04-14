import json
import os
import time
from datetime import timedelta

import arrow
import flask_profiler
import sentry_sdk
from coinbase_commerce.error import WebhookInvalidPayload, SignatureVerificationError
from coinbase_commerce.webhook import Webhook
from flask import (
    Flask,
    redirect,
    url_for,
    render_template,
    request,
    jsonify,
    flash,
    session,
    g,
)
from flask_admin import Admin
from flask_cors import cross_origin, CORS
from flask_login import current_user
from sentry_sdk.integrations.flask import FlaskIntegration
from sentry_sdk.integrations.sqlalchemy import SqlalchemyIntegration
from werkzeug.middleware.proxy_fix import ProxyFix

from app import paddle_utils, config
from app.admin_model import (
    SLAdminIndexView,
    UserAdmin,
    EmailLogAdmin,
    AliasAdmin,
    MailboxAdmin,
    ManualSubscriptionAdmin,
    CouponAdmin,
    CustomDomainAdmin,
    AdminAuditLogAdmin,
    TransactionalComplaintAdmin,
)
from app.api.base import api_bp
from app.auth.base import auth_bp
from app.config import (
    DB_URI,
    FLASK_SECRET,
    SENTRY_DSN,
    URL,
    SHA1,
    PADDLE_MONTHLY_PRODUCT_ID,
    FLASK_PROFILER_PATH,
    FLASK_PROFILER_PASSWORD,
    SENTRY_FRONT_END_DSN,
    FIRST_ALIAS_DOMAIN,
    SESSION_COOKIE_NAME,
    PLAUSIBLE_HOST,
    PLAUSIBLE_DOMAIN,
    GITHUB_CLIENT_ID,
    GOOGLE_CLIENT_ID,
    FACEBOOK_CLIENT_ID,
    LANDING_PAGE_URL,
    STATUS_PAGE_URL,
    SUPPORT_EMAIL,
    PADDLE_MONTHLY_PRODUCT_IDS,
    PADDLE_YEARLY_PRODUCT_IDS,
    PGP_SIGNER,
    COINBASE_WEBHOOK_SECRET,
    PAGE_LIMIT,
    PADDLE_COUPON_ID,
    ZENDESK_ENABLED,
)
from app.dashboard.base import dashboard_bp
from app.db import Session
from app.developer.base import developer_bp
from app.discover.base import discover_bp
from app.email_utils import send_email, render
from app.extensions import login_manager, migrate, limiter
from app.fake_data import fake_data
from app.jose_utils import get_jwk_key
from app.log import LOG
from app.models import (
    User,
    Alias,
    Subscription,
    PlanEnum,
    CustomDomain,
    Mailbox,
    CoinbaseSubscription,
    EmailLog,
    Contact,
    ManualSubscription,
    Coupon,
    AdminAuditLog,
    TransactionalComplaint,
)
from app.monitor.base import monitor_bp
from app.oauth.base import oauth_bp
from app.phone.base import phone_bp
from app.utils import random_string

if SENTRY_DSN:
    LOG.d("enable sentry")
    sentry_sdk.init(
        dsn=SENTRY_DSN,
        release=f"app@{SHA1}",
        integrations=[
            FlaskIntegration(),
            SqlalchemyIntegration(),
        ],
    )

# the app is served behind nginx which uses http and not https
os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"


def create_light_app() -> Flask:
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = DB_URI
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    @app.teardown_appcontext
    def shutdown_session(response_or_exc):
        Session.remove()

    return app


def create_app() -> Flask:
    app = Flask(__name__)
    # SimpleLogin is deployed behind NGINX
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_host=1)
    limiter.init_app(app)

    app.url_map.strict_slashes = False

    app.config["SQLALCHEMY_DATABASE_URI"] = DB_URI
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    # enable to print all queries generated by sqlalchemy
    # app.config["SQLALCHEMY_ECHO"] = True

    app.secret_key = FLASK_SECRET

    app.config["TEMPLATES_AUTO_RELOAD"] = True

    # to have a "fluid" layout for admin
    app.config["FLASK_ADMIN_FLUID_LAYOUT"] = True

    # to avoid conflict with other cookie
    app.config["SESSION_COOKIE_NAME"] = SESSION_COOKIE_NAME
    if URL.startswith("https"):
        app.config["SESSION_COOKIE_SECURE"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

    setup_error_page(app)

    init_extensions(app)
    register_blueprints(app)
    set_index_page(app)
    jinja2_filter(app)

    setup_favicon_route(app)
    setup_openid_metadata(app)

    init_admin(app)
    setup_paddle_callback(app)
    setup_coinbase_commerce(app)
    setup_do_not_track(app)
    register_custom_commands(app)

    if FLASK_PROFILER_PATH:
        LOG.d("Enable flask-profiler")
        app.config["flask_profiler"] = {
            "enabled": True,
            "storage": {"engine": "sqlite", "FILE": FLASK_PROFILER_PATH},
            "basicAuth": {
                "enabled": True,
                "username": "admin",
                "password": FLASK_PROFILER_PASSWORD,
            },
            "ignore": ["^/static/.*", "/git", "/exception"],
        }
        flask_profiler.init_app(app)

    # enable CORS on /api endpoints
    CORS(app, resources={r"/api/*": {"origins": "*"}})

    # set session to permanent so user stays signed in after quitting the browser
    # the cookie is valid for 7 days
    @app.before_request
    def make_session_permanent():
        session.permanent = True
        app.permanent_session_lifetime = timedelta(days=7)

    @app.teardown_appcontext
    def cleanup(resp_or_exc):
        Session.remove()

    return app


@login_manager.user_loader
def load_user(alternative_id):
    user = User.get_by(alternative_id=alternative_id)
    if user and user.disabled:
        return None

    return user


def register_blueprints(app: Flask):
    app.register_blueprint(auth_bp)
    app.register_blueprint(monitor_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(developer_bp)
    app.register_blueprint(phone_bp)

    app.register_blueprint(oauth_bp, url_prefix="/oauth")
    app.register_blueprint(oauth_bp, url_prefix="/oauth2")

    app.register_blueprint(discover_bp)
    app.register_blueprint(api_bp)


def set_index_page(app):
    @app.route("/", methods=["GET", "POST"])
    def index():
        if current_user.is_authenticated:
            return redirect(url_for("dashboard.index"))
        else:
            return redirect(url_for("auth.login"))

    @app.before_request
    def before_request():
        # not logging /static call
        if (
            not request.path.startswith("/static")
            and not request.path.startswith("/admin/static")
            and not request.path.startswith("/_debug_toolbar")
        ):
            g.start_time = time.time()

            # to handle the referral url that has ?slref=code part
            ref_code = request.args.get("slref")
            if ref_code:
                session["slref"] = ref_code

    @app.after_request
    def after_request(res):
        # not logging /static call
        if (
            not request.path.startswith("/static")
            and not request.path.startswith("/admin/static")
            and not request.path.startswith("/_debug_toolbar")
            and not request.path.startswith("/git")
            and not request.path.startswith("/favicon.ico")
        ):
            LOG.d(
                "%s %s %s %s %s, takes %s",
                request.remote_addr,
                request.method,
                request.path,
                request.args,
                res.status_code,
                time.time() - g.start_time,
            )

        return res


def setup_openid_metadata(app):
    @app.route("/.well-known/openid-configuration")
    @cross_origin()
    def openid_config():
        res = {
            "issuer": URL,
            "authorization_endpoint": URL + "/oauth2/authorize",
            "token_endpoint": URL + "/oauth2/token",
            "userinfo_endpoint": URL + "/oauth2/userinfo",
            "jwks_uri": URL + "/jwks",
            "response_types_supported": [
                "code",
                "token",
                "id_token",
                "id_token token",
                "id_token code",
            ],
            "subject_types_supported": ["public"],
            "id_token_signing_alg_values_supported": ["RS256"],
            # todo: add introspection and revocation endpoints
            # "introspection_endpoint": URL + "/oauth2/token/introspection",
            # "revocation_endpoint": URL + "/oauth2/token/revocation",
        }

        return jsonify(res)

    @app.route("/jwks")
    @cross_origin()
    def jwks():
        res = {"keys": [get_jwk_key()]}
        return jsonify(res)


def get_current_user():
    try:
        return g.user
    except AttributeError:
        return current_user


def setup_error_page(app):
    @app.errorhandler(400)
    def bad_request(e):
        if request.path.startswith("/api/"):
            return jsonify(error="Bad Request"), 400
        else:
            return render_template("error/400.html"), 400

    @app.errorhandler(401)
    def unauthorized(e):
        if request.path.startswith("/api/"):
            return jsonify(error="Unauthorized"), 401
        else:
            flash("You need to login to see this page", "error")
            return redirect(url_for("auth.login", next=request.full_path))

    @app.errorhandler(403)
    def forbidden(e):
        if request.path.startswith("/api/"):
            return jsonify(error="Forbidden"), 403
        else:
            return render_template("error/403.html"), 403

    @app.errorhandler(429)
    def rate_limited(e):
        LOG.w(
            "Client hit rate limit on path %s, user:%s",
            request.path,
            get_current_user(),
        )
        if request.path.startswith("/api/"):
            return jsonify(error="Rate limit exceeded"), 429
        else:
            return render_template("error/429.html"), 429

    @app.errorhandler(404)
    def page_not_found(e):
        if request.path.startswith("/api/"):
            return jsonify(error="No such endpoint"), 404
        else:
            return render_template("error/404.html"), 404

    @app.errorhandler(405)
    def wrong_method(e):
        if request.path.startswith("/api/"):
            return jsonify(error="Method not allowed"), 405
        else:
            return render_template("error/405.html"), 405

    @app.errorhandler(Exception)
    def error_handler(e):
        LOG.e(e)
        if request.path.startswith("/api/"):
            return jsonify(error="Internal error"), 500
        else:
            return render_template("error/500.html"), 500


def setup_favicon_route(app):
    @app.route("/favicon.ico")
    def favicon():
        return redirect("/static/favicon.ico")


def jinja2_filter(app):
    def format_datetime(value):
        dt = arrow.get(value)
        return dt.humanize()

    app.jinja_env.filters["dt"] = format_datetime

    @app.context_processor
    def inject_stage_and_region():
        return dict(
            YEAR=arrow.now().year,
            URL=URL,
            SENTRY_DSN=SENTRY_FRONT_END_DSN,
            VERSION=SHA1,
            FIRST_ALIAS_DOMAIN=FIRST_ALIAS_DOMAIN,
            PLAUSIBLE_HOST=PLAUSIBLE_HOST,
            PLAUSIBLE_DOMAIN=PLAUSIBLE_DOMAIN,
            GITHUB_CLIENT_ID=GITHUB_CLIENT_ID,
            GOOGLE_CLIENT_ID=GOOGLE_CLIENT_ID,
            FACEBOOK_CLIENT_ID=FACEBOOK_CLIENT_ID,
            LANDING_PAGE_URL=LANDING_PAGE_URL,
            STATUS_PAGE_URL=STATUS_PAGE_URL,
            SUPPORT_EMAIL=SUPPORT_EMAIL,
            PGP_SIGNER=PGP_SIGNER,
            CANONICAL_URL=f"{URL}{request.path}",
            PAGE_LIMIT=PAGE_LIMIT,
            ZENDESK_ENABLED=ZENDESK_ENABLED,
        )


def setup_paddle_callback(app: Flask):
    @app.route("/paddle", methods=["GET", "POST"])
    def paddle():
        LOG.d(f"paddle callback {request.form.get('alert_name')} {request.form}")

        # make sure the request comes from Paddle
        if not paddle_utils.verify_incoming_request(dict(request.form)):
            LOG.e("request not coming from paddle. Request data:%s", dict(request.form))
            return "KO", 400

        if (
            request.form.get("alert_name") == "subscription_created"
        ):  # new user subscribes
            # the passthrough is json encoded, e.g.
            # request.form.get("passthrough") = '{"user_id": 88 }'
            passthrough = json.loads(request.form.get("passthrough"))
            user_id = passthrough.get("user_id")
            user = User.get(user_id)

            subscription_plan_id = int(request.form.get("subscription_plan_id"))

            if subscription_plan_id in PADDLE_MONTHLY_PRODUCT_IDS:
                plan = PlanEnum.monthly
            elif subscription_plan_id in PADDLE_YEARLY_PRODUCT_IDS:
                plan = PlanEnum.yearly
            else:
                LOG.e(
                    "Unknown subscription_plan_id %s %s",
                    subscription_plan_id,
                    request.form,
                )
                return "No such subscription", 400

            sub = Subscription.get_by(user_id=user.id)

            if not sub:
                LOG.d(f"create a new Subscription for user {user}")
                Subscription.create(
                    user_id=user.id,
                    cancel_url=request.form.get("cancel_url"),
                    update_url=request.form.get("update_url"),
                    subscription_id=request.form.get("subscription_id"),
                    event_time=arrow.now(),
                    next_bill_date=arrow.get(
                        request.form.get("next_bill_date"), "YYYY-MM-DD"
                    ).date(),
                    plan=plan,
                )
            else:
                LOG.d(f"Update an existing Subscription for user {user}")
                sub.cancel_url = request.form.get("cancel_url")
                sub.update_url = request.form.get("update_url")
                sub.subscription_id = request.form.get("subscription_id")
                sub.event_time = arrow.now()
                sub.next_bill_date = arrow.get(
                    request.form.get("next_bill_date"), "YYYY-MM-DD"
                ).date()
                sub.plan = plan

                # make sure to set the new plan as not-cancelled
                # in case user cancels a plan and subscribes a new plan
                sub.cancelled = False

            LOG.d("User %s upgrades!", user)

            Session.commit()

        elif request.form.get("alert_name") == "subscription_payment_succeeded":
            subscription_id = request.form.get("subscription_id")
            LOG.d("Update subscription %s", subscription_id)

            sub: Subscription = Subscription.get_by(subscription_id=subscription_id)
            # when user subscribes, the "subscription_payment_succeeded" can arrive BEFORE "subscription_created"
            # at that time, subscription object does not exist yet
            if sub:
                sub.event_time = arrow.now()
                sub.next_bill_date = arrow.get(
                    request.form.get("next_bill_date"), "YYYY-MM-DD"
                ).date()

                Session.commit()

        elif request.form.get("alert_name") == "subscription_cancelled":
            subscription_id = request.form.get("subscription_id")

            sub: Subscription = Subscription.get_by(subscription_id=subscription_id)
            if sub:
                # cancellation_effective_date should be the same as next_bill_date
                LOG.w(
                    "Cancel subscription %s %s on %s, next bill date %s",
                    subscription_id,
                    sub.user,
                    request.form.get("cancellation_effective_date"),
                    sub.next_bill_date,
                )
                sub.event_time = arrow.now()

                sub.cancelled = True
                Session.commit()

                user = sub.user

                send_email(
                    user.email,
                    "SimpleLogin - your subscription is canceled",
                    render(
                        "transactional/subscription-cancel.txt",
                        end_date=request.form.get("cancellation_effective_date"),
                    ),
                )

            else:
                return "No such subscription", 400
        elif request.form.get("alert_name") == "subscription_updated":
            subscription_id = request.form.get("subscription_id")

            sub: Subscription = Subscription.get_by(subscription_id=subscription_id)
            if sub:
                LOG.d(
                    "Update subscription %s %s on %s, next bill date %s",
                    subscription_id,
                    sub.user,
                    request.form.get("cancellation_effective_date"),
                    sub.next_bill_date,
                )
                if (
                    int(request.form.get("subscription_plan_id"))
                    == PADDLE_MONTHLY_PRODUCT_ID
                ):
                    plan = PlanEnum.monthly
                else:
                    plan = PlanEnum.yearly

                sub.cancel_url = request.form.get("cancel_url")
                sub.update_url = request.form.get("update_url")
                sub.event_time = arrow.now()
                sub.next_bill_date = arrow.get(
                    request.form.get("next_bill_date"), "YYYY-MM-DD"
                ).date()
                sub.plan = plan

                # make sure to set the new plan as not-cancelled
                sub.cancelled = False

                Session.commit()
            else:
                return "No such subscription", 400
        elif request.form.get("alert_name") == "payment_refunded":
            subscription_id = request.form.get("subscription_id")
            LOG.d("Refund request for subscription %s", subscription_id)

            sub: Subscription = Subscription.get_by(subscription_id=subscription_id)

            if sub:
                user = sub.user
                Subscription.delete(sub.id)
                Session.commit()
                LOG.e("%s requests a refund", user)

        return "OK"

    @app.route("/paddle_coupon", methods=["GET", "POST"])
    def paddle_coupon():
        LOG.d(f"paddle coupon callback %s", request.form)

        if not paddle_utils.verify_incoming_request(dict(request.form)):
            LOG.e("request not coming from paddle. Request data:%s", dict(request.form))
            return "KO", 400

        product_id = request.form.get("p_product_id")
        if product_id != PADDLE_COUPON_ID:
            LOG.e("product_id %s not match with %s", product_id, PADDLE_COUPON_ID)
            return "KO", 400

        email = request.form.get("email")
        LOG.d("Paddle coupon request for %s", email)

        coupon = Coupon.create(
            code=random_string(30),
            comment="For 1-year coupon",
            expires_date=arrow.now().shift(years=1, days=-1),
            commit=True,
        )

        return (
            f"Your 1-year coupon is <b>{coupon.code}</b> <br> "
            f"It's valid until <b>{coupon.expires_date.date().isoformat()}</b>"
        )


def setup_coinbase_commerce(app):
    @app.route("/coinbase", methods=["POST"])
    def coinbase_webhook():
        # event payload
        request_data = request.data.decode("utf-8")
        # webhook signature
        request_sig = request.headers.get("X-CC-Webhook-Signature", None)

        try:
            # signature verification and event object construction
            event = Webhook.construct_event(
                request_data, request_sig, COINBASE_WEBHOOK_SECRET
            )
        except (WebhookInvalidPayload, SignatureVerificationError) as e:
            LOG.e("Invalid Coinbase webhook")
            return str(e), 400

        LOG.d("Coinbase event %s", event)

        if event["type"] == "charge:confirmed":
            if handle_coinbase_event(event):
                return "success", 200
            else:
                return "error", 400

        return "success", 200


def handle_coinbase_event(event) -> bool:
    user_id = int(event["data"]["metadata"]["user_id"])
    code = event["data"]["code"]
    user = User.get(user_id)
    if not user:
        LOG.e("User not found %s", user_id)
        return False

    coinbase_subscription: CoinbaseSubscription = CoinbaseSubscription.get_by(
        user_id=user_id
    )

    if not coinbase_subscription:
        LOG.d("Create a coinbase subscription for %s", user)
        coinbase_subscription = CoinbaseSubscription.create(
            user_id=user_id, end_at=arrow.now().shift(years=1), code=code, commit=True
        )
        send_email(
            user.email,
            "Your SimpleLogin account has been upgraded",
            render(
                "transactional/coinbase/new-subscription.txt",
                coinbase_subscription=coinbase_subscription,
            ),
            render(
                "transactional/coinbase/new-subscription.html",
                coinbase_subscription=coinbase_subscription,
            ),
        )
    else:
        if coinbase_subscription.code != code:
            LOG.d("Update code from %s to %s", coinbase_subscription.code, code)
            coinbase_subscription.code = code

        if coinbase_subscription.is_active():
            coinbase_subscription.end_at = coinbase_subscription.end_at.shift(years=1)
        else:  # already expired subscription
            coinbase_subscription.end_at = arrow.now().shift(years=1)

        Session.commit()

        send_email(
            user.email,
            "Your SimpleLogin account has been extended",
            render(
                "transactional/coinbase/extend-subscription.txt",
                coinbase_subscription=coinbase_subscription,
            ),
            render(
                "transactional/coinbase/extend-subscription.html",
                coinbase_subscription=coinbase_subscription,
            ),
        )

    return True


def init_extensions(app: Flask):
    login_manager.init_app(app)
    migrate.init_app(app)


def init_admin(app):
    admin = Admin(name="SimpleLogin", template_mode="bootstrap4")

    admin.init_app(app, index_view=SLAdminIndexView())
    admin.add_view(UserAdmin(User, Session))
    admin.add_view(AliasAdmin(Alias, Session))
    admin.add_view(MailboxAdmin(Mailbox, Session))
    admin.add_view(EmailLogAdmin(EmailLog, Session))
    admin.add_view(CouponAdmin(Coupon, Session))
    admin.add_view(ManualSubscriptionAdmin(ManualSubscription, Session))
    admin.add_view(CustomDomainAdmin(CustomDomain, Session))
    admin.add_view(AdminAuditLogAdmin(AdminAuditLog, Session))
    admin.add_view(TransactionalComplaintAdmin(TransactionalComplaint, Session))


def register_custom_commands(app):
    """
    Adhoc commands run during data migration.
    Registered as flask command, so it can run as:

    > flask {task-name}
    """

    @app.cli.command("fill-up-email-log-alias")
    def fill_up_email_log_alias():
        """Fill up email_log.alias_id column"""
        # split all emails logs into 1000-size trunks
        nb_email_log = EmailLog.count()
        LOG.d("total trunks %s", nb_email_log // 1000 + 2)
        for trunk in reversed(range(1, nb_email_log // 1000 + 2)):
            nb_update = 0
            for email_log, contact in (
                Session.query(EmailLog, Contact)
                .filter(EmailLog.contact_id == Contact.id)
                .filter(EmailLog.id <= trunk * 1000)
                .filter(EmailLog.id > (trunk - 1) * 1000)
                .filter(EmailLog.alias_id.is_(None))
            ):
                email_log.alias_id = contact.alias_id
                nb_update += 1

            LOG.d("finish trunk %s, update %s email logs", trunk, nb_update)
            Session.commit()

    @app.cli.command("dummy-data")
    def dummy_data():
        from init_app import add_sl_domains

        LOG.w("reset db, add fake data")
        fake_data()
        add_sl_domains()


def setup_do_not_track(app):
    @app.route("/dnt")
    def do_not_track():
        return """
        <script src="/static/local-storage-polyfill.js"></script>

        <script>
// Disable Analytics if this script is called

store.set('analytics-ignore', 't');

alert("Analytics disabled");

window.location.href = "/";

</script>
        """


def local_main():
    config.COLOR_LOG = True
    app = create_app()

    # enable flask toolbar
    from flask_debugtoolbar import DebugToolbarExtension

    app.config["DEBUG_TB_PROFILER_ENABLED"] = True
    app.config["DEBUG_TB_INTERCEPT_REDIRECTS"] = False
    app.debug = True
    DebugToolbarExtension(app)

    # disable the sqlalchemy debug panels because of "IndexError: pop from empty list" from:
    # duration = time.time() - conn.info['query_start_time'].pop(-1)
    # app.config["DEBUG_TB_PANELS"] += ("flask_debugtoolbar_sqlalchemy.SQLAlchemyPanel",)

    app.run(debug=True, port=7777)

    # uncomment to run https locally
    # LOG.d("enable https")
    # import ssl
    # context = ssl.SSLContext(ssl.PROTOCOL_TLSv1_2)
    # context.load_cert_chain("local_data/cert.pem", "local_data/key.pem")
    # app.run(debug=True, port=7777, ssl_context=context)


if __name__ == "__main__":
    local_main()
