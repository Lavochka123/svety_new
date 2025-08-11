# svety/web/__init__.py
from __future__ import annotations

from datetime import datetime, timezone

from flask import Flask, jsonify, render_template

from svety.core.config import cfg

app = Flask(__name__, template_folder="templates", static_folder="static")
app.config["SECRET_KEY"] = cfg.SECRET_KEY


@app.get("/")
def index():
    """Главная страница."""
    return render_template("index.html", year=datetime.now().year)


@app.get("/healthz")
def healthz():
    """Пробный эндпоинт для проверки живости приложения."""
    return jsonify(status="ok", ts=datetime.now(timezone.utc).isoformat())


__all__ = ["app"]
