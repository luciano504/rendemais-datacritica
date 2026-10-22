# -*- coding: utf-8 -*-
"""
EXCEDENTE DA REBAIXA NO VR -> ressarcimento do fornecedor (Data Crítica)

Regra do Luciano (22/10/2026): a rebaixa do Data Crítica vale até o vencimento OU
até vender a quantidade do lote, o que vier primeiro. No VR quem devolve o preço
normal é o servidor de preços (precos_vr.php), que confere as vendas a cada 5 min.
O que sair NO PREÇO DA REBAIXA além da quantidade do lote, nesse intervalo, também
é cobrado do fornecedor.

De onde vem: apps.preco_fila no banco do VR (o servidor grava lá a quantidade-limite,
o vendido no preço da rebaixa e o motivo do encerramento). Lido pelo App SQL (só SELECT).

Para onde vai: ressarcimento_diario, uma linha por lote no DIA SEGUINTE ao
encerramento — data própria para não sobrescrever a linha FEFO que o sync grava no
dia em que o lote zerou (a chave da tabela é lote_id+data). Só grava quando esse
dia já chegou. É idempotente: rodar de novo regrava o mesmo valor.
"""
import os, io, time
from datetime import date, timedelta
from urllib.parse import urlencode
import requests
import pandas as pd

VR_URL = os.environ["VR_URL"]
SUPABASE_URL = "https://estciwkeihmokvlnvaum.supabase.co"
SUPABASE_KEY = os.environ.get("SUPABASE_KEY") or "sb_publishable_l14fjxQmWUeu5OXlZJ35GA_-X3ESkgZ"
SB = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}", "Content-Type": "application/json"}


def query_vr(sql, tentativas=4):
    ultimo = None
    for t in range(1, tentativas + 1):
        try:
            r = requests.post(VR_URL, data=urlencode({"sql_query": sql, "export_type": "csv"}),
                              headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=300)
            r.raise_for_status()
            ini = r.text.lstrip()[:100].lower()
            if ini.startswith("<!doctype") or ini.startswith("<html") or "fatal error" in ini:
                if "streamCsv" in r.text and "firstRow" in r.text:   # consulta sem linhas
                    return pd.DataFrame()
                raise RuntimeError("Erro VR: " + r.text[:200])
            df = pd.read_csv(io.StringIO(r.text))
            df.columns = [str(c).lstrip("﻿").strip() for c in df.columns]
            return df
        except Exception as e:
            ultimo = e
            time.sleep(8 * t)
    raise RuntimeError(f"Query VR falhou: {ultimo}")


def sb_get(path, params):
    r = requests.get(f"{SUPABASE_URL}/rest/v1/{path}?{params}", headers=SB, timeout=60)
    r.raise_for_status()
    return r.json()


def num(v, d=0.0):
    try:
        f = float(v)
        return f if f == f else d
    except (TypeError, ValueError):
        return d


def main():
    exc = query_vr("""
        select ref, id_loja, preco, preconormal, qtd_limite, vendido, encerrado_em::date as dia
        from apps.preco_fila
        where origem = 'datacritica' and tipo = 'oferta' and qtd_limite is not null
          and encerrado_em is not null and encerrado_motivo like 'vendeu%'
          and vendido > qtd_limite and encerrado_em > now() - interval '60 days'""")
    if not len(exc):
        print("Nenhuma rebaixa passou da quantidade no VR.")
        return
    hoje = date.today()
    alvo = {}
    for _, e in exc.iterrows():
        partes = str(e["ref"]).split()[0].split(":")
        if len(partes) >= 3 and partes[0] == "dc" and partes[1]:
            alvo[partes[1]] = e
    if not alvo:
        print("Excedentes sem lote identificado.")
        return
    lotes = {l["id"]: l for l in sb_get("lotes", "select=id,ean,loja&id=in.(" + ",".join(alvo) + ")")}
    linhas = []
    for lid, e in alvo.items():
        l = lotes.get(lid)
        if not l:
            continue
        q = num(e["vendido"]) - num(e["qtd_limite"])
        normal, pv = num(e["preconormal"]), num(e["preco"])
        if q <= 0 or normal <= pv:
            continue
        dia = date.fromisoformat(str(e["dia"])[:10]) + timedelta(days=1)
        if dia > hoje:
            continue   # grava amanhã
        pl = sb_get("produtos_loja", f"select=id_fornecedor,nome_fornecedor&ean=eq.{l['ean']}&loja=eq.{l['loja']}")
        pl = pl[0] if pl else {}
        linhas.append({
            "lote_id": lid, "ean": l["ean"], "loja": l["loja"],
            "id_fornecedor": pl.get("id_fornecedor"), "nome_fornecedor": pl.get("nome_fornecedor"),
            "data": dia.isoformat(), "qtd_vendida": round(q, 3),
            "preco_normal": round(normal, 4), "preco_vigente": round(pv, 4),
            "valor": round((normal - pv) * q, 2)})
    if not linhas:
        print("Nada a gravar hoje.")
        return
    r = requests.post(f"{SUPABASE_URL}/rest/v1/ressarcimento_diario?on_conflict=lote_id,data",
                      headers=dict(SB, Prefer="resolution=merge-duplicates,return=minimal"), json=linhas, timeout=120)
    if r.status_code >= 300:
        raise RuntimeError(f"Upsert ressarcimento_diario: {r.status_code} {r.text[:300]}")
    total = sum(x["valor"] for x in linhas)
    print(f"Excedente da rebaixa no VR: {len(linhas)} lote(s), R$ {total:.2f} lançados para os fornecedores.")
    resumo = os.environ.get("GITHUB_STEP_SUMMARY")
    if resumo:
        with open(resumo, "a") as f:
            f.write(f"### Excedente da rebaixa no VR\n{len(linhas)} lote(s) · R$ {total:.2f} cobrados dos fornecedores\n")


if __name__ == "__main__":
    main()
