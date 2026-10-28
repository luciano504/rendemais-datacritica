# -*- coding: utf-8 -*-
"""
REBAIXA ENCERRADA POR QUANTIDADE NO VR -> lote zerado no Data Crítica (na hora)

Pedido do Luciano (28/10/2026): quando o servidor de preços (precos_vr.php) devolve o
preço normal porque a quantidade da rebaixa foi vendida, o gerente precisa saber logo
para tirar a placa. O aviso "Retirar placa de rebaixa" do app aparece quando o lote
está zerado, mas o sync de vendas só roda 07h e 16h. Este script roda junto com o
relógio de 5 em 5 minutos e marca o lote como zerado assim que o VR encerra a rebaixa.

Só mexe em lote ainda aberto (zerado=false e resolvido=false). Idempotente.
Usa só a biblioteca padrão do Python (o relógio não instala nada).
"""
import os, io, csv, json, urllib.request, urllib.parse

VR_URL = os.environ["VR_URL"]
SUPABASE_URL = "https://estciwkeihmokvlnvaum.supabase.co"
SUPABASE_KEY = os.environ.get("SUPABASE_KEY") or "sb_publishable_l14fjxQmWUeu5OXlZJ35GA_-X3ESkgZ"
SB = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}", "Content-Type": "application/json"}


def query_vr(sql):
    body = urllib.parse.urlencode({"sql_query": sql, "export_type": "csv"}).encode()
    req = urllib.request.Request(VR_URL, data=body, headers={"Content-Type": "application/x-www-form-urlencoded"})
    txt = urllib.request.urlopen(req, timeout=60).read().decode("utf-8-sig", "replace")
    ini = txt.lstrip()[:100].lower()
    if ini.startswith("<!doctype") or ini.startswith("<html") or "fatal error" in ini:
        if "streamCsv" in txt and "firstRow" in txt:   # consulta sem linhas
            return []
        raise RuntimeError("Erro VR: " + txt[:200])
    return list(csv.DictReader(io.StringIO(txt)))


def sb(method, path, dados=None, extra=None):
    req = urllib.request.Request(f"{SUPABASE_URL}/rest/v1/{path}", method=method,
                                 data=json.dumps(dados).encode() if dados is not None else None,
                                 headers=dict(SB, **(extra or {})))
    r = urllib.request.urlopen(req, timeout=60).read()
    return json.loads(r) if r else None


def main():
    encerradas = query_vr("""
        select ref, id_loja, vendido, qtd_limite, to_char(encerrado_em, 'DD/MM HH24:MI') as quando
        from apps.preco_fila
        where origem = 'datacritica' and tipo = 'oferta' and encerrado_motivo like 'vendeu%'
          and encerrado_em > now() - interval '3 days'""")
    ids = {}
    for e in encerradas:
        p = str(e.get("ref", "")).split()[0].split(":")
        if len(p) >= 3 and p[0] == "dc" and p[1]:
            ids[p[1]] = e
    if not ids:
        print("Nenhuma rebaixa encerrada por quantidade nos últimos 3 dias.")
        return
    abertos = sb("GET", "lotes?select=id,loja,ean,qtd_restante&zerado=eq.false&resolvido=eq.false&id=in.(" + ",".join(ids) + ")")
    if not abertos:
        print(f"{len(ids)} rebaixa(s) encerrada(s) por quantidade — todas já zeradas no app.")
        return
    for l in abertos:
        sb("PATCH", f"lotes?id=eq.{l['id']}&zerado=eq.false", {"zerado": True, "qtd_restante": 0},
           {"Prefer": "return=minimal"})
        e = ids[l["id"]]
        print(f"Zerado: {l['loja']} · EAN {l['ean']} · vendeu {e.get('vendido')} de {e.get('qtd_limite')} no VR ({e.get('quando')})")
    resumo = os.environ.get("GITHUB_STEP_SUMMARY")
    if resumo:
        with open(resumo, "a") as f:
            f.write(f"Rebaixa vendida no VR → lote zerado no app (aviso de placa): {len(abertos)}\n")


if __name__ == "__main__":
    main()
