# bfocus-monitor

Monitoramento de erros do [bFocus](https://bfocus.com.br) para **Python**: os erros não tratados do
seu sistema chegam ao bFocus, são agrupados entre todos os clientes e viram demanda para a equipe.

Zero dependências (só biblioteca padrão) · Python 3.9+ · envio em segundo plano · nunca derruba o app.

## Instalar

```bash
pip install bfocus-monitor
```

## Ligar (uma linha)

```python
import os
import bfocus_monitor

bfocus_monitor.init(key="bf_mon_…", release="1.4.2", environment="production",
                    signing_secret=os.environ.get("BFOCUS_SIGNING_SECRET"))
```

A chave do agente está no bFocus em **Monitoramento → Agentes**. O `init` não espera a rede: o sinal
de vida (o painel mostra o agente vivo mesmo sem erro) sai numa thread em segundo plano, no `init` e
a cada 5 min. Ele liga a captura de:

- exceção não tratada no topo (`sys.excepthook`, nível `fatal`) — o Python continua quebrando e
  imprimindo o traceback como sempre;
- exceção que mata uma thread (`threading.excepthook`);
- exceção que o loop do asyncio só registraria (quando o `init` roda dentro do loop; fora dele use
  `bfocus_monitor.install_asyncio_handler(loop)`);
- no encerramento do processo, envia o que ficou na fila (até 2 s).

Opções: `base_url`, `sample_rate` (0..1), `ignore` (textos ou `re.Pattern`), `before_send`
(recebe o evento e devolve o evento alterado ou `None` para descartar), `auto_capture=False` (sem
ganchos globais) e `in_app_prefixes` (módulos seus mesmo instalados em site-packages).

## Framework

**FastAPI / Starlette** (qualquer app ASGI):

```python
from bfocus_monitor.asgi import BfocusMiddleware

app.add_middleware(BfocusMiddleware)
```

**Django**:

```python
MIDDLEWARE = [..., "bfocus_monitor.django.BfocusMiddleware"]
```

**Flask**:

```python
from bfocus_monitor.flask import init_app

init_app(app)
```

As integrações abrem um escopo por requisição (dois usuários simultâneos nunca trocam de identidade),
registram a rota (`transaction`) e a URL **sem** query string, capturam a exceção e deixam o
framework responder como sempre.

## Quem foi afetado (identidade)

```python
bfocus_monitor.set_user(external_id=user.id, customer_external_id=user.company_id)
```

Com `signing_secret` no `init` (o segredo da chave de assinatura do sistema, o mesmo do `userHash`
do widget) o pacote assina a identidade sozinho. Sem ele, passe o `user_hash` que o seu servidor já
gera: `set_user(user.id, user.company_id, user_hash=...)`. Sem assinatura válida o erro conta como
"não identificado". `bfocus_monitor.sign_user(secret, user_id, customer_id)` gera o mesmo hash para
entregar ao front.

## Manual

```python
try:
    emitir_nota()
except Exception as exc:
    bfocus_monitor.capture_exception(exc, tags={"modulo": "fiscal"})
    # ou, dentro do except: bfocus_monitor.capture_exception()

bfocus_monitor.capture_message("estoque negativo", level="warning")
bfocus_monitor.set_tag("filial", "POA")
bfocus_monitor.add_breadcrumb("fiscal", "XML assinado", "info")
bfocus_monitor.flush(timeout=2.0)   # CLI, serverless, fim de job
```

## O que é enviado

Tipo e mensagem da exceção (encadeadas: vai a causa raiz, com `(dentro de: …)`), os frames (de fora
para dentro, caminho relativo ao diretório atual, `inApp` falso para biblioteca), release, ambiente,
rota, URL sem query, identidade, tags, passos e versão do Python/SO. Nunca: corpo de requisição,
cookies, headers. O mesmo erro sai no máximo 1 vez a cada 30 s, e no máximo 100 eventos por minuto.

## Testes

```bash
python -m unittest discover -s tests -t tests
```

Licença MIT — Berni Software.
