import os

from flask import Flask, jsonify

from core_services.audit_api import audit_api
from core_services.common import install_request_context, register_error_handlers
from core_services.reports import reports
from core_services.users import users
from core_services.works import works


def create_app():
    app = Flask(__name__)
    install_request_context(app)
    register_error_handlers(app)
    app.register_blueprint(reports)
    app.register_blueprint(users)
    app.register_blueprint(works)
    app.register_blueprint(audit_api)

    @app.get("/health")
    def health():
        return jsonify({"status": "ok", "service": "urban-alert-core"})

    return app


app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")))