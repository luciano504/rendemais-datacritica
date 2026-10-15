# -*- coding: utf-8 -*-
"""
RISCO DE VENCIMENTO — análise proativa do Data Crítica
=======================================================
Lê a validade que as lojas digitam no recebimento (VR: notaentradaitemvalidade),
cruza com o estoque atual e a venda média diária, e projeta, lote a lote, o que
NÃO vai vender antes de vencer (descontada a folga de 5 dias do app; em produto de
vida curta a folga é 1/5 da vida útil — carne resfriada de 10 dias tem folga de 2).

Regras:
  * Estoque atual é distribuído entre os lotes recebidos do de validade mais
    longa para o mais curto (FEFO: o que sobrou na loja é o que chegou depois).
    Estoque sem validade registrada fica de fora (não inventa lote).
  * VMD: 90 dias quando o item tem 90 dias de venda; senão, 30 dias.
  * Venda projetada: os lotes vendem em fila, do que vence primeiro para o
    último; cada um tem até (validade - 5 dias) para zerar.
  * Sobra × custo com imposto = valor em risco.
  * Urgência pelo shelf do próprio produto: "agir" quando faltam até metade
    da vida útil (mínimo 15 dias) — iogurte entra cedo, arroz entra tarde —
    ou quando vai sobrar metade do lote ou mais faltando até 60 dias.
  * Ajustes contra alerta falso: VMD = maior entre a regra acima e a de 30
    dias (venda acelerando); só a fração do estoque que entrou com validade é
    distribuída nos lotes; estoque acima de 1,5× o recebido em 90 dias é
    marcado como suspeito; vida útil fora de 0,5×–2× o padrão do produto vira
    "conferir validade" (provável erro de digitação). Esses dois últimos não
    entram em "agir agora".
  * Fica de fora: seção MATÉRIA PRIMA e lojas desativadas (6, 7, 10).

Grava a tabela risco_validade (apaga e regrava inteira a cada rodada).
Log é público (repo público): só imprime contagens, nunca valores ou nomes.
"""
import csv, io, os, sys, time
from datetime import date, datetime
import requests

VR_URL = os.environ["VR_URL"]
SB_URL = "https://estciwkeihmokvlnvaum.supabase.co"
# a chave publicável é a mesma do app (pública); usa a secreta se existir
SB_KEY = os.environ.get("SUPABASE_KEY") or "sb_publishable_l14fjxQmWUeu5OXlZJ35GA_-X3ESkgZ"
LOJAS = {1: "L01", 2: "L02", 3: "L03", 4: "L04", 5: "L05", 8: "L08", 9: "L09"}
IDS = ",".join(str(i) for i in LOJAS)
FOLGA = 5
SECOES_FORA = {"MATERIA PRIMA", "MATÉRIA PRIMA"}

HJ = {"apikey": SB_KEY, "Authorization": "Bearer " + SB_KEY, "Content-Type": "application/json"}


def vr(sql, tent=3):
    for i in range(tent):
        try:
            r = requests.post(VR_URL, data={"sql_query": sql, "export_type": "csv"}, timeout=300)
            t = r.text.lstrip("﻿")
            if "<html" in t[:500].lower() or "fatal error" in t.lower()[:2000]:
                raise RuntimeError("VR devolveu HTML/erro em vez de CSV")
            return list(csv.DictReader(io.StringIO(t)))
        except Exception as e:
            print(f"  consulta falhou ({i+1}/{tent}): {type(e).__name__}")
            time.sleep(20)
    raise RuntimeError("VR não respondeu")


def sb_get(tab, params):
    out, ini = [], 0
    while True:
        h = dict(HJ, Range=f"{ini}-{ini+999}")
        r = requests.get(f"{SB_URL}/rest/v1/{tab}", headers=h, params=params, timeout=120)
        r.raise_for_status()
        b = r.json()
        out += b
        if len(b) < 1000:
            return out
        ini += 1000


def num(x, d=0.0):
    try:
        return float(x) if x not in (None, "") else d
    except ValueError:
        return d


def main():
    hoje = date.today()
    d = lambda s: (datetime.strptime(s[:10], "%Y-%m-%d").date() - hoje).days

    print("1/4 lotes do recebimento…")
    lotes = vr(f"""
      select ne.id_loja loja, nei.id_produto, v.datavalidade validade,
             min(ne.dataentrada) entrada,
             sum(v.quantidade*greatest(coalesce(v.qtdembalagem,1),1)) qtd
      from notaentradaitemvalidade v
      join notaentradaitem nei on nei.id = v.id_notaentradaitem
      join notaentrada ne on ne.id = nei.id_notaentrada
      where coalesce(v.id_situacaocadastro,1) = 1
        and v.datavalidade >= current_date
        and v.datavalidade > ne.dataentrada
        and ne.dataentrada >= current_date - 365
        and ne.id_loja in ({IDS})
      group by 1,2,3""")
    print(f"   {len(lotes)} lotes")

    # vida útil típica de cada produto (todas as lojas, 365 dias, inclusive lotes
    # já vencidos) e cobertura: quanto do que entrou na loja nos últimos 90 dias
    # veio com validade (90 e não 365: a coleta só normalizou recentemente)
    tipico = vr(f"""
      select nei.id_produto, count(*) n,
             percentile_disc(0.5) within group (order by v.datavalidade - ne.dataentrada) shelf_med
      from notaentradaitemvalidade v
      join notaentradaitem nei on nei.id = v.id_notaentradaitem
      join notaentrada ne on ne.id = nei.id_notaentrada
      where coalesce(v.id_situacaocadastro,1) = 1 and v.datavalidade > ne.dataentrada
        and ne.dataentrada >= current_date - 365 and ne.id_loja in ({IDS})
      group by 1""")
    SHELF = {r["id_produto"]: (int(num(r["n"])), num(r["shelf_med"])) for r in tipico}
    cobert = vr(f"""
      select ne.id_loja loja, nei.id_produto,
             sum(nei.quantidade*greatest(coalesce(nei.qtdembalagem,1),1)) recebido,
             sum(coalesce(vv.q,0)) com_validade
      from notaentradaitem nei
      join notaentrada ne on ne.id = nei.id_notaentrada
      left join (select id_notaentradaitem, sum(quantidade*greatest(coalesce(qtdembalagem,1),1)) q
                 from notaentradaitemvalidade where coalesce(id_situacaocadastro,1) = 1
                 group by 1) vv on vv.id_notaentradaitem = nei.id
      where ne.dataentrada >= current_date - 90 and ne.id_loja in ({IDS})
        and nei.id_produto in (select nei2.id_produto from notaentradaitemvalidade v2
                               join notaentradaitem nei2 on nei2.id = v2.id_notaentradaitem
                               join notaentrada ne2 on ne2.id = nei2.id_notaentrada
                               where v2.datavalidade >= current_date and ne2.dataentrada >= current_date - 365)
      group by 1,2""")
    COB = {(r["loja"], r["id_produto"]): (num(r["recebido"]), num(r["com_validade"])) for r in cobert}

    print("2/4 venda média diária…")
    vend = vr(f"""
      select v.id_loja loja, vi.id_produto,
             sum(vi.quantidade)/90.0 vmd90,
             sum(vi.quantidade) filter (where vi.data >= current_date-30)/30.0 vmd30,
             min(vi.data) primeira
      from pdv.vendaitem vi join pdv.venda v on v.id = vi.id_venda
      where vi.data >= current_date-90 and vi.data < current_date
        and not vi.cancelado and not v.cancelado and v.id_loja in ({IDS})
      group by 1,2""")
    print(f"   {len(vend)} produto×loja com venda")

    print("3/4 estoque, custo e cadastro…")
    cad = vr(f"""
      select pc.id_loja loja, pc.id_produto, pc.estoque, pc.custocomimposto custo,
             pc.precovenda preco, p.descricaocompleta descricao, mc.descricao secao
      from produtocomplemento pc
      join produto p on p.id = pc.id_produto
      left join mercadologico mc on mc.mercadologico1 = p.mercadologico1 and mc.nivel = 1
      where pc.id_loja in ({IDS}) and pc.estoque > 0""")
    ids_prod = sorted({r["id_produto"] for r in lotes})
    eans_vr = {}
    for i in range(0, len(ids_prod), 3000):
        bloco = ",".join(ids_prod[i:i+3000])
        for r in vr(f"select id_produto, codigobarras from produtoautomacao where id_produto in ({bloco})"):
            eans_vr.setdefault(r["id_produto"], []).append(str(r["codigobarras"]).strip())
    # prefere o EAN que o Data Crítica já usa para o produto
    ja = {}
    for p in sb_get("produtos", {"select": "ean,id_produto"}):
        if p.get("id_produto") is not None:
            ja.setdefault(str(p["id_produto"]), p["ean"])

    def ean_de(idp):
        if idp in ja:
            return ja[idp]
        cs = [c for c in eans_vr.get(idp, []) if c.isdigit()]
        if not cs:
            return None
        cs.sort(key=lambda c: (len(c) in (13, 8, 14, 12), len(c), c), reverse=True)
        return cs[0]

    V = {(r["loja"], r["id_produto"]): r for r in vend}
    P = {(r["loja"], r["id_produto"]): r for r in cad}
    G = {}
    for r in lotes:
        G.setdefault((r["loja"], r["id_produto"]), []).append(r)

    print("4/4 projetando…")
    agora = datetime.utcnow().isoformat() + "Z"
    linhas = []
    for k, ls in G.items():
        p = P.get(k)
        if not p:
            continue
        sec = (p.get("secao") or "").strip().upper()
        if sec in SECOES_FORA:
            continue
        est = num(p["estoque"])
        if est <= 0:
            continue
        ean = ean_de(k[1])
        if not ean:
            continue
        v = V.get(k) or {}
        historico = bool(v.get("primeira")) and d(v["primeira"]) <= -80
        vmd = num(v.get("vmd90")) if historico else num(v.get("vmd30"), num(v.get("vmd90")))
        # ajuste 1: venda acelerando (oferta, sazonal, item ganhando giro) — vale o ritmo mais alto
        vmd = max(vmd, num(v.get("vmd30")), 0.0)

        # ajuste 3: só a parte do estoque que entrou com validade vai para os lotes;
        # estoque muito acima de tudo o que entrou em 90 dias é suspeito (fantasma)
        recebido, com_val = COB.get(k, (0.0, 0.0))
        cobertura = min(1.0, com_val / recebido) if recebido > 0 else 1.0
        estoque_suspeito = recebido > 0 and est > 1.5 * recebido
        est_alocar = min(est, recebido) if estoque_suspeito else est
        est_alocar *= cobertura

        # estoque nos lotes: do de validade mais longa para o mais curto
        ls = sorted(ls, key=lambda r: (r["validade"], r["entrada"]), reverse=True)
        resto = est_alocar
        for l in ls:
            l["q"] = min(num(l["qtd"]), max(resto, 0))
            resto -= l["q"]
        vivos = sorted([l for l in ls if l["q"] > 0.0001], key=lambda r: r["validade"])

        t = 0.0  # dias de venda já "gastos" pelos lotes anteriores
        for l in vivos:
            dias = d(l["validade"])
            shelf = max(1, (datetime.strptime(l["validade"], "%Y-%m-%d").date()
                            - datetime.strptime(l["entrada"][:10], "%Y-%m-%d").date()).days)
            # folga do app (5 dias), proporcional em produto de vida curta: 1/5 do shelf
            folga = max(1, min(FOLGA, round(shelf / 5)))
            lim = max(0, dias - folga)
            cap = max(0.0, vmd * (lim - t)) if vmd > 0 else 0.0
            vende = min(l["q"], cap)
            sobra = l["q"] - vende
            if vmd > 0:
                t += vende / vmd
            if sobra <= 0.0001:
                continue
            custo = num(p["custo"])
            # ajuste 2: vida útil muito diferente do padrão do produto = provável
            # erro de digitação no recebimento (ano/mês trocado)
            n_tip, med = SHELF.get(k[1], (0, 0))
            conferir = n_tip >= 3 and med > 0 and (shelf < 0.5 * med or shelf > 2 * med)
            urgente = dias <= max(15, shelf * 0.5) or (sobra >= 0.5 * l["q"] and dias <= 60)
            linhas.append({
                "loja": LOJAS[int(k[0])], "ean": ean, "id_produto": int(k[1]),
                "descricao": p.get("descricao"), "secao": p.get("secao"),
                "entrada": l["entrada"][:10], "validade": l["validade"], "dias": dias,
                "shelf": shelf, "folga": folga, "qtd_lote": round(l["q"], 3), "sobra": round(sobra, 3),
                "estoque": round(est, 3), "vmd": round(vmd, 4),
                "dias_para_zerar": round(l["q"] / vmd, 1) if vmd > 0 else None,
                "custo": round(custo, 4), "preco": round(num(p.get("preco")), 2),
                "valor_risco": round(sobra * custo, 2),
                "sem_venda": vmd <= 0,
                # agir: já passou da metade da vida útil (mín. 15 dias) ou vai
                # sobrar metade do lote ou mais com até 60 dias para vencer
                "urgente": urgente and not conferir and not estoque_suspeito,
                "conferir_validade": conferir, "shelf_tipico": int(med) if med else None,
                "estoque_suspeito": estoque_suspeito, "cobertura": round(cobertura, 3),
                "atualizado_em": agora,
            })

    # mesmo produto com dois EANs no cadastro → evita PK repetida
    uniq = {}
    for r in linhas:
        kk = (r["loja"], r["ean"], r["validade"])
        if kk in uniq:
            a = uniq[kk]
            for c in ("qtd_lote", "sobra", "valor_risco"):
                a[c] = round(a[c] + r[c], 3)
        else:
            uniq[kk] = r
    linhas = list(uniq.values())

    r = requests.delete(f"{SB_URL}/rest/v1/risco_validade", headers=HJ, params={"loja": "neq.__"}, timeout=120)
    if r.status_code >= 300:
        print("Falha ao limpar risco_validade:", r.status_code, r.text[:300])
        sys.exit(1)
    for i in range(0, len(linhas), 500):
        r = requests.post(f"{SB_URL}/rest/v1/risco_validade", headers=dict(HJ, Prefer="return=minimal"),
                          json=linhas[i:i+500], timeout=120)
        if r.status_code >= 300:
            print("Falha ao gravar:", r.status_code, r.text[:300])
            sys.exit(1)

    n_urg = sum(1 for x in linhas if x["urgente"])
    n_conf = sum(1 for x in linhas if x["conferir_validade"])
    msg = f"Risco de vencimento: {len(linhas)} lotes com sobra projetada ({n_urg} para agir agora, {n_conf} para conferir validade)."
    print(msg)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write(msg + "\n")


if __name__ == "__main__":
    main()
