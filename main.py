from fastapi import FastAPI

app = FastAPI(title="Sistema de Ponto", version="1.0")
from fastapi import FastAPI
from fastapi import FastAPI, HTTPException, Depends, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel
from typing import Optional
from fastapi import FastAPI
import sqlite3
import secrets
import csv
import io
from datetime import datetime, date
from fastapi.responses import StreamingResponse

app = FastAPI(title="Sistema de Ponto", version="1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

security = HTTPBasic()

# ─── Credenciais dos supervisores (em produção, use banco de dados) 
SUPERVISORES = {
    "supervisor": "senha123",
    "admin": "admin2024",
}

# ─── Banco de Dados 
def get_db():
    conn = sqlite3.connect("ponto.db", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    conn.executescript("""
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
    conn.close()

init_db()

# ─── Modelos 
class LoginRequest(BaseModel):
    matricula: str
    nome: Optional[str] = None

class EventoRequest(BaseModel):
    matricula: str
    sessao_id: int
    tipo: str  # PAUSA_INICIO, PAUSA_FIM, LOGOUT

class SyncRequest(BaseModel):
    matricula: str
    sessao_id: int
    total_logado_seg: int
    total_pausa_seg: int

# ─── Auth supervisor ──────────────────────────────────────────────────────────
def verificar_supervisor(credentials: HTTPBasicCredentials = Depends(security)):
    senha_correta = SUPERVISORES.get(credentials.username)
    if not senha_correta or not secrets.compare_digest(credentials.password, senha_correta):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Credenciais inválidas",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username

# ─── Rotas do Colaborador ─────────────────────────────────────────────────────

@app.post("/api/login")
def login(req: LoginRequest):
    conn = get_db()
    try:
        # Salvar/atualizar colaborador
        conn.execute(
            "INSERT OR IGNORE INTO colaboradores (matricula, nome) VALUES (?, ?)",
            (req.matricula, req.nome or req.matricula)
        )
        if req.nome:
            conn.execute(
                "UPDATE colaboradores SET nome = ? WHERE matricula = ?",
                (req.nome, req.matricula)
            )

        agora = datetime.utcnow().isoformat()
        hoje = date.today().isoformat()

        # Fechar sessão aberta anterior se houver
        conn.execute(
            """UPDATE sessoes SET status = 'incompleto', logout_em = ?
               WHERE matricula = ? AND status = 'ativo'""",
            (agora, req.matricula)
        )

        # Criar nova sessão
        cur = conn.execute(
            "INSERT INTO sessoes (matricula, data, login_em, status) VALUES (?, ?, ?, 'ativo')",
            (req.matricula, hoje, agora)
        )
        sessao_id = cur.lastrowid

        conn.execute(
            "INSERT INTO eventos (sessao_id, matricula, tipo, timestamp) VALUES (?, ?, 'LOGIN', ?)",
            (sessao_id, req.matricula, agora)
        )
        conn.commit()

        return {"ok": True, "sessao_id": sessao_id, "login_em": agora}
    finally:
        conn.close()


@app.post("/api/evento")
def registrar_evento(req: EventoRequest):
    conn = get_db()
    try:
        agora = datetime.utcnow().isoformat()
        conn.execute(
            "INSERT INTO eventos (sessao_id, matricula, tipo, timestamp) VALUES (?, ?, ?, ?)",
            (req.sessao_id, req.matricula, req.tipo, agora)
        )
        conn.commit()
        return {"ok": True, "timestamp": agora}
    finally:
        conn.close()


@app.post("/api/sync")
def sincronizar(req: SyncRequest):
    """Recebe os contadores do frontend periodicamente (a cada 30s)."""
    conn = get_db()
    try:
        conn.execute(
            """UPDATE sessoes SET total_logado_seg = ?, total_pausa_seg = ?
               WHERE id = ? AND matricula = ?""",
            (req.total_logado_seg, req.total_pausa_seg, req.sessao_id, req.matricula)
        )
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


@app.post("/api/logout")
def logout(req: SyncRequest):
    conn = get_db()
    try:
        agora = datetime.utcnow().isoformat()
        conn.execute(
            """UPDATE sessoes
               SET status = 'encerrado', logout_em = ?,
                   total_logado_seg = ?, total_pausa_seg = ?
               WHERE id = ? AND matricula = ?""",
            (agora, req.total_logado_seg, req.total_pausa_seg, req.sessao_id, req.matricula)
        )
        conn.execute(
            "INSERT INTO eventos (sessao_id, matricula, tipo, timestamp) VALUES (?, ?, 'LOGOUT', ?)",
            (req.sessao_id, req.matricula, agora)
        )
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


# ─── Rotas do Supervisor ──────────────────────────────────────────────────────

@app.get("/api/supervisor/hoje")
def relatorio_hoje(usuario: str = Depends(verificar_supervisor)):
    conn = get_db()
    try:
        hoje = date.today().isoformat()
        rows = conn.execute(
            """SELECT s.id, s.matricula, c.nome, s.login_em, s.logout_em,
                      s.total_logado_seg, s.total_pausa_seg, s.status
               FROM sessoes s
               JOIN colaboradores c ON c.matricula = s.matricula
               WHERE s.data = ?
               ORDER BY s.login_em DESC""",
            (hoje,)
        ).fetchall()

        resultado = []
        for r in rows:
            logado = r["total_logado_seg"] or 0
            pausa = r["total_pausa_seg"] or 0
            resultado.append({
                "sessao_id": r["id"],
                "matricula": r["matricula"],
                "nome": r["nome"],
                "login_em": r["login_em"],
                "logout_em": r["logout_em"],
                "total_logado_seg": logado,
                "total_pausa_seg": pausa,
                "total_trabalhado_seg": max(0, logado - pausa),
                "status": r["status"],
            })
        return resultado
    finally:
        conn.close()


@app.get("/api/supervisor/historico")
def historico(data: Optional[str] = None, usuario: str = Depends(verificar_supervisor)):
    conn = get_db()
    try:
        filtro_data = data or date.today().isoformat()
        rows = conn.execute(
            """SELECT s.id, s.matricula, c.nome, s.data, s.login_em, s.logout_em,
                      s.total_logado_seg, s.total_pausa_seg, s.status
               FROM sessoes s
               JOIN colaboradores c ON c.matricula = s.matricula
               WHERE s.data = ?
               ORDER BY s.login_em""",
            (filtro_data,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@app.get("/api/supervisor/csv")
def exportar_csv(data: Optional[str] = None, usuario: str = Depends(verificar_supervisor)):
    conn = get_db()
    try:
        filtro_data = data or date.today().isoformat()
        rows = conn.execute(
            """SELECT s.matricula, c.nome, s.data, s.login_em, s.logout_em,
                      s.total_logado_seg, s.total_pausa_seg,
                      MAX(0, s.total_logado_seg - s.total_pausa_seg) as trabalhado,
                      s.status
               FROM sessoes s
               JOIN colaboradores c ON c.matricula = s.matricula
               WHERE s.data = ?
               ORDER BY s.matricula""",
            (filtro_data,)
        ).fetchall()

        def fmt(seg):
            if not seg:
                return "00:00:00"
            h = seg // 3600
            m = (seg % 3600) // 60
            s = seg % 60
            return f"{h:02d}:{m:02d}:{s:02d}"

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["matricula", "nome", "data", "login", "logout",
                         "tempo_logado", "tempo_pausa", "tempo_trabalhado", "status"])
        for r in rows:
            writer.writerow([
                r["matricula"], r["nome"], r["data"],
                r["login_em"], r["logout_em"] or "",
                fmt(r["total_logado_seg"]), fmt(r["total_pausa_seg"]),
                fmt(r["trabalhado"]), r["status"]
            ])

        output.seek(0)
        return StreamingResponse(
            iter([output.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename=ponto_{filtro_data}.csv"}
        )
    finally:
        conn.close()


@app.get("/api/supervisor/eventos/{sessao_id}")
def eventos_sessao(sessao_id: int, usuario: str = Depends(verificar_supervisor)):
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT tipo, timestamp FROM eventos WHERE sessao_id = ? ORDER BY timestamp",
            (sessao_id,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@app.get("/")
def root():
    return {"status": "Sistema de Ponto rodando", "docs": "/docs"}