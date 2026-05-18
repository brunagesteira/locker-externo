from fastapi import FastAPI, HTTPException, Depends, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel
from typing import Optional, Literal
from pathlib import Path
import sqlite3
import secrets
import csv
import io
from datetime import datetime, date, timezone
from fastapi.responses import StreamingResponse

app = FastAPI(title="Sistema de Ponto", version="3.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

security = HTTPBasic()

SUPERVISORES = {
    "supervisor": "senha123",
    "admin": "admin2024",
}

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "ponto.db"


def agora_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso_agora() -> str:
    return agora_utc().isoformat()


def parse_dt(valor: str) -> datetime:
    dt = datetime.fromisoformat(valor)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    try:
        conn.executescript("""
            PRAGMA journal_mode=WAL;

            CREATE TABLE IF NOT EXISTS colaboradores (
                matricula TEXT PRIMARY KEY,
                nome TEXT
            );

            CREATE TABLE IF NOT EXISTS sessoes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                matricula TEXT NOT NULL,
                data TEXT NOT NULL,
                login_em TEXT NOT NULL,
                logout_em TEXT,
                total_logado_seg INTEGER DEFAULT 0,
                total_pausa_seg INTEGER DEFAULT 0,
                status TEXT DEFAULT 'ativo'
            );

            CREATE TABLE IF NOT EXISTS eventos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sessao_id INTEGER NOT NULL,
                matricula TEXT NOT NULL,
                tipo TEXT NOT NULL,
                timestamp TEXT NOT NULL
            );
        """)
        conn.commit()
    finally:
        conn.close()


init_db()


class LoginRequest(BaseModel):
    matricula: str
    nome: Optional[str] = None


class EventoRequest(BaseModel):
    matricula: str
    sessao_id: int
    tipo: Literal["PAUSA_INICIO", "PAUSA_FIM"]


class LogoutRequest(BaseModel):
    matricula: str
    sessao_id: int


def verificar_supervisor(credentials: HTTPBasicCredentials = Depends(security)):
    senha_correta = SUPERVISORES.get(credentials.username)
    if not senha_correta or not secrets.compare_digest(credentials.password, senha_correta):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Credenciais inválidas",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


def buscar_sessao(conn, sessao_id: int, matricula: Optional[str] = None):
    if matricula:
        sessao = conn.execute(
            "SELECT * FROM sessoes WHERE id = ? AND matricula = ?",
            (sessao_id, matricula)
        ).fetchone()
    else:
        sessao = conn.execute("SELECT * FROM sessoes WHERE id = ?", (sessao_id,)).fetchone()

    if not sessao:
        raise HTTPException(status_code=404, detail="Sessão não encontrada")

    return sessao


def calcular_totais(conn, sessao_id: int, agora: Optional[datetime] = None):
    agora = agora or agora_utc()
    sessao = buscar_sessao(conn, sessao_id)

    login_em = parse_dt(sessao["login_em"])
    fim = parse_dt(sessao["logout_em"]) if sessao["logout_em"] else agora

    total_logado = max(0, int((fim - login_em).total_seconds()))

    eventos = conn.execute(
        """
        SELECT tipo, timestamp
        FROM eventos
        WHERE sessao_id = ?
          AND tipo IN ('PAUSA_INICIO', 'PAUSA_FIM')
        ORDER BY timestamp ASC, id ASC
        """,
        (sessao_id,)
    ).fetchall()

    total_pausa = 0
    pausa_aberta = None

    for ev in eventos:
        ts = parse_dt(ev["timestamp"])

        if ev["tipo"] == "PAUSA_INICIO" and pausa_aberta is None:
            pausa_aberta = ts

        elif ev["tipo"] == "PAUSA_FIM" and pausa_aberta is not None:
            total_pausa += max(0, int((ts - pausa_aberta).total_seconds()))
            pausa_aberta = None

    em_pausa = pausa_aberta is not None and sessao["status"] == "ativo"

    if pausa_aberta is not None:
        total_pausa += max(0, int((fim - pausa_aberta).total_seconds()))

    total_pausa = min(total_pausa, total_logado)

    return {
        "sessao_id": sessao["id"],
        "sessaoId": sessao["id"],
        "matricula": sessao["matricula"],
        "data": sessao["data"],
        "login_em": sessao["login_em"],
        "logout_em": sessao["logout_em"],
        "total_logado_seg": total_logado,
        "total_pausa_seg": total_pausa,
        "status": "em_pausa" if em_pausa else sessao["status"],
        "em_pausa": em_pausa,
    }


def salvar_totais_sessao(conn, sessao_id: int):
    totais = calcular_totais(conn, sessao_id)
    conn.execute(
        """
        UPDATE sessoes
        SET total_logado_seg = ?, total_pausa_seg = ?
        WHERE id = ?
        """,
        (totais["total_logado_seg"], totais["total_pausa_seg"], sessao_id)
    )
    return totais


@app.get("/")
def root():
    return {
        "status": "Sistema de Ponto rodando",
        "db_path": str(DB_PATH),
        "docs": "/docs",
    }


@app.get("/api/health")
def health():
    return {"ok": True, "db_path": str(DB_PATH)}


@app.post("/api/login")
def login(req: LoginRequest):
    conn = get_db()
    try:
        matricula = req.matricula.strip()
        nome = (req.nome or matricula).strip()

        if not matricula:
            raise HTTPException(status_code=400, detail="Matrícula é obrigatória")

        agora = iso_agora()
        hoje = date.today().isoformat()

        conn.execute(
            """
            INSERT INTO colaboradores (matricula, nome)
            VALUES (?, ?)
            ON CONFLICT(matricula) DO UPDATE SET nome = excluded.nome
            """,
            (matricula, nome)
        )

        abertas = conn.execute(
            "SELECT id FROM sessoes WHERE matricula = ? AND status = 'ativo'",
            (matricula,)
        ).fetchall()

        for aberta in abertas:
            conn.execute(
                """
                UPDATE sessoes
                SET status = 'incompleto', logout_em = ?
                WHERE id = ?
                """,
                (agora, aberta["id"])
            )
            salvar_totais_sessao(conn, aberta["id"])

        cur = conn.execute(
            """
            INSERT INTO sessoes (matricula, data, login_em, status)
            VALUES (?, ?, ?, 'ativo')
            """,
            (matricula, hoje, agora)
        )

        sessao_id = cur.lastrowid

        conn.execute(
            """
            INSERT INTO eventos (sessao_id, matricula, tipo, timestamp)
            VALUES (?, ?, 'LOGIN', ?)
            """,
            (sessao_id, matricula, agora)
        )

        conn.commit()

        return {
            "ok": True,
            "sessao_id": sessao_id,
            "sessaoId": sessao_id,
            "login_em": agora,
            "matricula": matricula,
            "nome": nome,
        }
    finally:
        conn.close()


@app.get("/api/sessao/{sessao_id}")
def status_sessao(sessao_id: int, matricula: Optional[str] = None):
    conn = get_db()
    try:
        sessao = buscar_sessao(conn, sessao_id, matricula)
        totais = calcular_totais(conn, sessao_id)
        colaborador = conn.execute(
            "SELECT nome FROM colaboradores WHERE matricula = ?",
            (sessao["matricula"],)
        ).fetchone()
        totais["nome"] = colaborador["nome"] if colaborador else sessao["matricula"]
        return totais
    finally:
        conn.close()


@app.post("/api/evento")
def registrar_evento(req: EventoRequest):
    conn = get_db()
    try:
        sessao = buscar_sessao(conn, req.sessao_id, req.matricula)

        if sessao["status"] != "ativo":
            raise HTTPException(status_code=400, detail="Sessão não está ativa")

        estado = calcular_totais(conn, req.sessao_id)
        ja_em_pausa = estado["em_pausa"]

        if req.tipo == "PAUSA_INICIO" and ja_em_pausa:
            return {"ok": True, "duplicado": True, **estado}

        if req.tipo == "PAUSA_FIM" and not ja_em_pausa:
            return {"ok": True, "duplicado": True, **estado}

        agora = iso_agora()

        conn.execute(
            """
            INSERT INTO eventos (sessao_id, matricula, tipo, timestamp)
            VALUES (?, ?, ?, ?)
            """,
            (req.sessao_id, req.matricula, req.tipo, agora)
        )

        totais = salvar_totais_sessao(conn, req.sessao_id)
        conn.commit()

        return {"ok": True, "timestamp": agora, "tipo": req.tipo, **totais}
    finally:
        conn.close()


@app.post("/api/logout")
def logout(req: LogoutRequest):
    conn = get_db()
    try:
        sessao = buscar_sessao(conn, req.sessao_id, req.matricula)

        if sessao["status"] != "ativo":
            totais = calcular_totais(conn, req.sessao_id)
            return {"ok": True, "ja_encerrada": True, **totais}

        agora = iso_agora()

        conn.execute(
            """
            INSERT INTO eventos (sessao_id, matricula, tipo, timestamp)
            VALUES (?, ?, 'LOGOUT', ?)
            """,
            (req.sessao_id, req.matricula, agora)
        )

        conn.execute(
            """
            UPDATE sessoes
            SET logout_em = ?, status = 'encerrado'
            WHERE id = ?
            """,
            (agora, req.sessao_id)
        )

        totais = salvar_totais_sessao(conn, req.sessao_id)
        conn.commit()

        return {"ok": True, **totais}
    finally:
        conn.close()


@app.get("/api/supervisor")
def supervisor(usuario: str = Depends(verificar_supervisor)):
    conn = get_db()
    try:
        sessoes = conn.execute(
            """
            SELECT
                s.id,
                s.matricula,
                COALESCE(c.nome, s.matricula) AS nome,
                s.data,
                s.login_em,
                s.logout_em,
                s.status
            FROM sessoes s
            LEFT JOIN colaboradores c ON c.matricula = s.matricula
            ORDER BY s.login_em DESC
            """
        ).fetchall()

        resposta = []

        for s in sessoes:
            totais = calcular_totais(conn, s["id"])
            resposta.append({
                "sessao_id": s["id"],
                "matricula": s["matricula"],
                "nome": s["nome"],
                "data": s["data"],
                "login_em": s["login_em"],
                "logout_em": s["logout_em"],
                "total_logado_seg": totais["total_logado_seg"],
                "total_pausa_seg": totais["total_pausa_seg"],
                "status": totais["status"],
            })

        return resposta
    finally:
        conn.close()


@app.get("/api/supervisor/csv")
def supervisor_csv(usuario: str = Depends(verificar_supervisor)):
    dados = supervisor(usuario)

    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";")
    writer.writerow([
        "sessao_id",
        "matricula",
        "nome",
        "data",
        "login_em",
        "logout_em",
        "total_logado_seg",
        "total_pausa_seg",
        "status",
    ])

    for item in dados:
        writer.writerow([
            item["sessao_id"],
            item["matricula"],
            item["nome"],
            item["data"],
            item["login_em"],
            item["logout_em"] or "",
            item["total_logado_seg"],
            item["total_pausa_seg"],
            item["status"],
        ])

    buffer.seek(0)

    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=ponto.csv"}
    )
