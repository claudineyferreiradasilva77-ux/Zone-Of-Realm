import hmac
import json
import os
import re
import secrets
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from pyramid.config import Configurator
from pyramid.response import Response
from waitress import serve

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("DB_PATH", str(BASE_DIR / "zorvanyx.db")))
CHAVE_PIX = os.environ.get("CHAVE_PIX", "").strip()
NOME_RECEBEDOR = os.environ.get("NOME_RECEBEDOR", "ZORVANYX SERVER").strip()
CIDADE_RECEBEDOR = os.environ.get("CIDADE_RECEBEDOR", "CACU").strip()
DISCORD_LINK = os.environ.get("DISCORD_LINK", "").strip()
ADMIN_KEY = os.environ.get("ADMIN_KEY", "").strip()
PLANOS = {"normal": {"nome": "VIP Normal", "valor": 8.00}, "premium": {"nome": "VIP Premium", "valor": 29.90}}
NICK_RE = re.compile(r"^[A-Za-z0-9_]{3,16}$")


def agora():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def iniciar_banco():
    with db() as con:
        con.execute("""CREATE TABLE IF NOT EXISTS pedidos (
            id TEXT PRIMARY KEY, nick TEXT NOT NULL, tipo_conta TEXT NOT NULL,
            plano TEXT NOT NULL, valor REAL NOT NULL, status TEXT NOT NULL,
            criado TEXT NOT NULL, confirmado TEXT, chat_token TEXT NOT NULL
        )""")
        con.execute("""CREATE TABLE IF NOT EXISTS mensagens (
            id INTEGER PRIMARY KEY AUTOINCREMENT, pedido_id TEXT NOT NULL,
            remetente TEXT NOT NULL CHECK(remetente IN ('COMPRADOR','ADMIN')),
            mensagem TEXT NOT NULL, criado TEXT NOT NULL,
            FOREIGN KEY(pedido_id) REFERENCES pedidos(id)
        )""")
        con.execute("""CREATE TABLE IF NOT EXISTS denuncias (
            id INTEGER PRIMARY KEY AUTOINCREMENT, pedido_id TEXT NOT NULL,
            motivo TEXT NOT NULL, detalhes TEXT NOT NULL, criado TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'NOVA',
            FOREIGN KEY(pedido_id) REFERENCES pedidos(id)
        )""")


def campo(tag, value):
    value = str(value)
    return f"{tag}{len(value):02d}{value}"


def crc16(payload):
    crc = 0xFFFF
    for byte in payload.encode("utf-8"):
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return f"{crc:04X}"


def gerar_pix(valor, txid):
    if not CHAVE_PIX:
        raise ValueError("A variável CHAVE_PIX não foi configurada no Render.")
    gui = campo("00", "br.gov.bcb.pix") + campo("01", CHAVE_PIX)
    txid = re.sub(r"[^A-Za-z0-9]", "", txid)[:25] or "ZORVANYX"
    payload = (campo("00", "01") + campo("26", gui) + campo("52", "0000") +
               campo("53", "986") + campo("54", f"{valor:.2f}") + campo("58", "BR") +
               campo("59", NOME_RECEBEDOR[:25].upper()) + campo("60", CIDADE_RECEBEDOR[:15].upper()) +
               campo("62", campo("05", txid)) + "6304")
    return payload + crc16(payload)


def json_response(data, status=200):
    return Response(json_body=data, status=status)


def body_json(request):
    try:
        return request.json_body
    except Exception:
        return {}


def admin_ok(request):
    sent = request.headers.get("X-Admin-Key", "")
    return bool(ADMIN_KEY) and hmac.compare_digest(sent, ADMIN_KEY)


def order_for_token(pedido_id, token):
    with db() as con:
        row = con.execute("SELECT * FROM pedidos WHERE id=?", (pedido_id,)).fetchone()
    if not row or not token or not hmac.compare_digest(row["chat_token"], token):
        return None
    return row


def home(request):
    return Response((BASE_DIR / "index.html").read_text(encoding="utf-8"), content_type="text/html", charset="utf-8")


def admin_page(request):
    return Response(ADMIN_HTML, content_type="text/html", charset="utf-8")


def criar_pedido(request):
    data = body_json(request)
    nick = str(data.get("nick", "")).strip()
    plano = str(data.get("plano", "")).lower()
    tipo = str(data.get("tipo_conta", "")).lower()
    if not NICK_RE.fullmatch(nick):
        return json_response({"erro": "Nick inválido. Use de 3 a 16 letras, números ou _.",}, 400)
    if plano not in PLANOS or tipo not in ("java_original", "tlauncher"):
        return json_response({"erro": "Confira o plano e o tipo de conta selecionados."}, 400)
    pedido_id = secrets.token_urlsafe(9)
    token = secrets.token_urlsafe(32)
    valor = PLANOS[plano]["valor"]
    try:
        pix = gerar_pix(valor, pedido_id[:20])
    except ValueError as exc:
        return json_response({"erro": str(exc)}, 503)
    with db() as con:
        con.execute("INSERT INTO pedidos(id,nick,tipo_conta,plano,valor,status,criado,chat_token) VALUES(?,?,?,?,?,?,?,?)",
                    (pedido_id, nick, tipo, plano, valor, "PENDENTE", agora(), token))
    return json_response({"id": pedido_id, "chat_token": token, "nick": nick,
                          "plano": PLANOS[plano]["nome"], "valor": valor,
                          "pix": pix, "discord": DISCORD_LINK,
                          "aviso": "O pagamento é conferido manualmente. Guarde o código do pedido e o acesso ao chat."}, 201)


def comprador_chat(request):
    pedido_id = request.matchdict["pedido_id"]
    token = request.headers.get("X-Chat-Token", "")
    order = order_for_token(pedido_id, token)
    if not order:
        return json_response({"erro": "Pedido ou acesso ao chat inválido."}, 403)
    if request.method == "POST":
        msg = str(body_json(request).get("mensagem", "")).strip()
        if not msg or len(msg) > 1000:
            return json_response({"erro": "A mensagem deve ter de 1 a 1000 caracteres."}, 400)
        with db() as con:
            con.execute("INSERT INTO mensagens(pedido_id,remetente,mensagem,criado) VALUES(?,?,?,?)",
                        (pedido_id, "COMPRADOR", msg, agora()))
    with db() as con:
        rows = con.execute("SELECT remetente,mensagem,criado FROM mensagens WHERE pedido_id=? ORDER BY id", (pedido_id,)).fetchall()
    return json_response({"pedido": {"id": order["id"], "nick": order["nick"], "plano": order["plano"], "status": order["status"]},
                          "mensagens": [dict(r) for r in rows]})


def denunciar(request):
    data = body_json(request)
    pedido_id = request.matchdict["pedido_id"]
    order = order_for_token(pedido_id, request.headers.get("X-Chat-Token", ""))
    if not order:
        return json_response({"erro": "Pedido ou acesso inválido."}, 403)
    motivo = str(data.get("motivo", "")).strip()[:80]
    detalhes = str(data.get("detalhes", "")).strip()[:1200]
    if not motivo or not detalhes:
        return json_response({"erro": "Informe o motivo e os detalhes da denúncia."}, 400)
    with db() as con:
        con.execute("INSERT INTO denuncias(pedido_id,motivo,detalhes,criado) VALUES(?,?,?,?)",
                    (pedido_id, motivo, detalhes, agora()))
    return json_response({"ok": True, "mensagem": "Denúncia enviada para análise."}, 201)


def admin_pedidos(request):
    if not admin_ok(request): return json_response({"erro": "Acesso não autorizado."}, 401)
    with db() as con:
        rows = con.execute("SELECT id,nick,tipo_conta,plano,valor,status,criado,confirmado FROM pedidos ORDER BY criado DESC LIMIT 200").fetchall()
    return json_response({"pedidos": [dict(r) for r in rows]})


def admin_confirmar(request):
    if not admin_ok(request): return json_response({"erro": "Acesso não autorizado."}, 401)
    pedido_id = request.matchdict["pedido_id"]
    with db() as con:
        cur = con.execute("UPDATE pedidos SET status='PAGO', confirmado=? WHERE id=? AND status='PENDENTE'", (agora(), pedido_id))
    if not cur.rowcount: return json_response({"erro": "Pedido não encontrado ou já processado."}, 404)
    return json_response({"ok": True, "mensagem": "Pagamento marcado como conferido. Faça a entrega manual no servidor."})


def admin_chats(request):
    if not admin_ok(request): return json_response({"erro": "Acesso não autorizado."}, 401)
    with db() as con:
        rows = con.execute("""SELECT p.id,p.nick,p.plano,p.status,
            (SELECT mensagem FROM mensagens m WHERE m.pedido_id=p.id ORDER BY m.id DESC LIMIT 1) ultima
            FROM pedidos p ORDER BY p.criado DESC LIMIT 200""").fetchall()
    return json_response({"chats": [dict(r) for r in rows]})


def admin_chat(request):
    if not admin_ok(request): return json_response({"erro": "Acesso não autorizado."}, 401)
    pedido_id = request.matchdict["pedido_id"]
    with db() as con:
        order = con.execute("SELECT id,nick,plano,status FROM pedidos WHERE id=?", (pedido_id,)).fetchone()
        if not order: return json_response({"erro": "Pedido não encontrado."}, 404)
        if request.method == "POST":
            msg = str(body_json(request).get("mensagem", "")).strip()
            if not msg or len(msg) > 1000: return json_response({"erro": "Mensagem inválida."}, 400)
            con.execute("INSERT INTO mensagens(pedido_id,remetente,mensagem,criado) VALUES(?,?,?,?)", (pedido_id, "ADMIN", msg, agora()))
        rows = con.execute("SELECT remetente,mensagem,criado FROM mensagens WHERE pedido_id=? ORDER BY id", (pedido_id,)).fetchall()
    return json_response({"pedido": dict(order), "mensagens": [dict(r) for r in rows]})


def admin_denuncias(request):
    if not admin_ok(request): return json_response({"erro": "Acesso não autorizado."}, 401)
    with db() as con:
        rows = con.execute("SELECT d.id,d.pedido_id,p.nick,d.motivo,d.detalhes,d.criado,d.status FROM denuncias d JOIN pedidos p ON p.id=d.pedido_id ORDER BY d.id DESC LIMIT 200").fetchall()
    return json_response({"denuncias": [dict(r) for r in rows]})


ADMIN_HTML = r'''<!doctype html><html lang="pt-BR"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>ZORVANYX • Administração</title><style>
*{box-sizing:border-box}body{margin:0;background:#050812;color:#eaf2ff;font:15px system-ui;padding:24px}.wrap{max-width:1100px;margin:auto}.panel{background:#0d1728;border:1px solid #233b5a;border-radius:18px;padding:20px;margin:16px 0}input,textarea,button{font:inherit;border-radius:10px;padding:11px;border:1px solid #29415f;background:#07101e;color:white}button{cursor:pointer;background:#087eaa;border:0;font-weight:700;margin:4px}button:hover{filter:brightness(1.2)}.muted{color:#9db0c9}.item{border-top:1px solid #26364b;padding:14px 0;display:flex;gap:12px;flex-wrap:wrap;align-items:center}.item strong{color:#55d8ff}.cols{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:12px}#login{display:flex;gap:8px;flex-wrap:wrap}textarea{width:100%;min-height:70px}pre{white-space:pre-wrap;word-break:break-word}
</style><div class="wrap"><h1>✦ ZORVANYX <span style="color:#55d8ff">ADMIN</span></h1><p class="muted">Pedidos, conversas privadas e denúncias. Confirme o PIX no banco antes de aprovar.</p><div class="panel" id="login"><input id="key" type="password" placeholder="Chave administrativa" autocomplete="current-password"><button onclick="entrar()">Entrar</button><span id="msg" class="muted"></span></div><div id="app" hidden><div class="cols"><section class="panel"><h2>Pedidos</h2><div id="orders"></div></section><section class="panel"><h2>Conversas</h2><div id="chats"></div><div id="chatbox"></div></section></div><section class="panel"><h2>Denúncias recebidas</h2><div id="reports"></div></section></div></div><script>
let key='';const H=()=>({'Content-Type':'application/json','X-Admin-Key':key});async function api(url,opt={}){let r=await fetch(url,{...opt,headers:{...H(),...(opt.headers||{})}});let d=await r.json();if(!r.ok)throw Error(d.erro||'Falha');return d}async function entrar(){key=document.getElementById('key').value;try{await carregar();document.getElementById('app').hidden=false;document.getElementById('msg').textContent='Acesso autorizado'}catch(e){document.getElementById('msg').textContent=e.message;key=''}}async function carregar(){let [p,c,d]=await Promise.all([api('/admin/pedidos'),api('/admin/chats'),api('/admin/denuncias')]);document.getElementById('orders').innerHTML=p.pedidos.map(x=>`<div class="item"><div><strong>${esc(x.nick)}</strong> • ${esc(x.plano)} • R$ ${Number(x.valor).toFixed(2)}<br><small>${esc(x.id)} | ${esc(x.tipo_conta)} | ${esc(x.status)}</small></div>${x.status==='PENDENTE'?`<button onclick="confirmar('${x.id}')">Confirmar PIX</button>`:''}<button onclick="abrirChat('${x.id}')">Abrir chat</button></div>`).join('')||'Nenhum pedido.';document.getElementById('chats').innerHTML=c.chats.map(x=>`<div class="item"><div><strong>${esc(x.nick)}</strong><br><small>${esc(x.id)} • ${esc(x.status)}</small></div><button onclick="abrirChat('${x.id}')">Conversar</button></div>`).join('')||'Sem conversas.';document.getElementById('reports').innerHTML=d.denuncias.map(x=>`<div class="item"><div><strong>#${x.id} • ${esc(x.nick)}</strong> — ${esc(x.motivo)}<br><small>${esc(x.pedido_id)} • ${esc(x.criado)}</small><p>${esc(x.detalhes)}</p></div></div>`).join('')||'Nenhuma denúncia.'}function esc(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}async function confirmar(id){if(!confirm('Você conferiu o PIX no banco e deseja marcar este pedido como pago?'))return;try{await api('/admin/confirmar/'+id,{method:'POST'});await carregar();alert('Pagamento confirmado. Faça a entrega manual no Minecraft.')}catch(e){alert(e.message)}}async function abrirChat(id){try{let d=await api('/admin/chat/'+id);document.getElementById('chatbox').innerHTML=`<h3>Chat: ${esc(d.pedido.nick)}</h3><div class="panel">${d.mensagens.map(m=>`<p><b>${esc(m.remetente)}:</b> ${esc(m.mensagem)}</p>`).join('')||'Sem mensagens ainda.'}</div><textarea id="reply" placeholder="Escreva para o comprador"></textarea><button onclick="responder('${id}')">Enviar mensagem</button>`}catch(e){alert(e.message)}}async function responder(id){let mensagem=document.getElementById('reply').value;try{await api('/admin/chat/'+id,{method:'POST',body:JSON.stringify({mensagem})});await abrirChat(id)}catch(e){alert(e.message)}}</script></html>'''


def main():
    iniciar_banco()
    with Configurator() as config:
        config.add_route("home", "/")
        config.add_route("order", "/api/pedidos")
        config.add_route("buyer_chat", "/api/chat/{pedido_id}")
        config.add_route("report", "/api/denuncia/{pedido_id}")
        config.add_route("admin", "/admin")
        config.add_route("admin_orders", "/admin/pedidos")
        config.add_route("admin_confirm", "/admin/confirmar/{pedido_id}")
        config.add_route("admin_chats", "/admin/chats")
        config.add_route("admin_chat", "/admin/chat/{pedido_id}")
        config.add_route("admin_reports", "/admin/denuncias")
        config.add_view(home, route_name="home")
        config.add_view(criar_pedido, route_name="order", request_method="POST")
        config.add_view(comprador_chat, route_name="buyer_chat", request_method=["GET", "POST"])
        config.add_view(denunciar, route_name="report", request_method="POST")
        config.add_view(admin_page, route_name="admin")
        config.add_view(admin_pedidos, route_name="admin_orders")
        config.add_view(admin_confirmar, route_name="admin_confirm", request_method="POST")
        config.add_view(admin_chats, route_name="admin_chats")
        config.add_view(admin_chat, route_name="admin_chat", request_method=["GET", "POST"])
        config.add_view(admin_denuncias, route_name="admin_reports")
        app = config.make_wsgi_app()
    port = int(os.environ.get("PORT", "8080"))
    print(f"ZORVANYX iniciado na porta {port}")
    serve(app, host="0.0.0.0", port=port)

if __name__ == "__main__":
    main()
