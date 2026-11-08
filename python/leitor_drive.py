#!/usr/bin/env python3
"""
Leitor serial de drives (inversores / servos) + relatório de máquina (movimento x parada).

Uso:
  python leitor_drive.py perfis
  python leitor_drive.py coletar  -c config.json [--porta COM3] [--simular] [--duracao 3600]
  python leitor_drive.py relatorio dados.csv -c config.json [--saida pasta]

A coleta grava um CSV continuamente (seguro para coletas longas). Ao encerrar
(Ctrl+C ou fim da duração), o relatório PDF + Excel é gerado automaticamente.
"""
import argparse, csv, json, math, os, random, re, sys, time
from datetime import datetime

AQUI = os.path.dirname(os.path.abspath(__file__))


# ----------------------------------------------------------------- configuração
def carregar_perfis():
    for p in (os.path.join(AQUI, "perfis.json"), os.path.join(AQUI, "..", "perfis.json")):
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                return {k: v for k, v in json.load(f).items() if not k.startswith("_")}
    sys.exit("perfis.json não encontrado ao lado do script.")


def carregar_config(caminho):
    with open(caminho, encoding="utf-8") as f:
        cfg = json.load(f)
    perfis = carregar_perfis()
    if "variaveis" not in cfg:
        p = perfis.get(cfg.get("perfil", "generico"))
        if not p:
            sys.exit(f"Perfil '{cfg.get('perfil')}' não existe. Rode: python leitor_drive.py perfis")
        cfg["variaveis"] = p["variaveis"]
        cfg.setdefault("var_movimento", p["var_movimento"])
        cfg.setdefault("limiar", p["limiar"])
        cfg.setdefault("perfil_nome", p["nome"])
    cfg.setdefault("modo", "modbus")
    cfg.setdefault("baud", 19200)
    cfg.setdefault("paridade", "N")
    cfg.setdefault("bits_dados", 8)
    cfg.setdefault("stop_bits", 1)
    cfg.setdefault("escravo", 1)
    cfg.setdefault("intervalo_ms", 500)
    cfg.setdefault("timeout_ms", 300)
    cfg.setdefault("confirmacao_s", 2)
    cfg.setdefault("ascii", {"formato": "kv", "separador": ";", "virgula_decimal": False})
    cfg.setdefault("maquina", {})
    return cfg


# ------------------------------------------------------------------ Modbus RTU
def crc16(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


class ModbusRTU:
    def __init__(self, ser, timeout_s):
        self.ser, self.timeout = ser, timeout_s

    def ler(self, escravo, fc, endereco, qtd):
        req = bytes([escravo, fc, endereco >> 8, endereco & 255, qtd >> 8, qtd & 255])
        c = crc16(req)
        req += bytes([c & 255, c >> 8])
        self.ser.reset_input_buffer()
        self.ser.write(req)
        esperado, buf, lim = 5 + 2 * qtd, b"", time.monotonic() + self.timeout
        while time.monotonic() < lim:
            buf += self.ser.read(esperado - len(buf) if len(buf) < esperado else 1)
            if len(buf) >= 5 and buf[1] & 0x80:
                raise IOError(f"Exceção Modbus {buf[2]} (end. {endereco})")
            if len(buf) >= esperado:
                break
        if len(buf) < esperado:
            raise IOError(f"Sem resposta (end. {endereco})")
        buf = buf[:esperado]
        c = crc16(buf[:-2])
        if buf[-2] != (c & 255) or buf[-1] != (c >> 8):
            raise IOError(f"CRC inválido (end. {endereco})")
        return [(buf[3 + 2 * i] << 8) | buf[4 + 2 * i] for i in range(qtd)]


def decodificar(v, regs):
    x = regs[0] * 65536 + regs[1] if v["tipo"].endswith("32") else regs[0]
    if v["tipo"] == "s16" and x > 32767:
        x -= 65536
    if v["tipo"] == "s32" and x > 2147483647:
        x -= 4294967296
    return x * float(v.get("escala", 1) or 1)


# ----------------------------------------------------------------------- ASCII
def num(s, virgula):
    if s is None:
        return None
    s = str(s).strip()
    if virgula:
        s = s.replace(".", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def parse_linha(linha, cfg):
    a, vs, val = cfg["ascii"], cfg["variaveis"], {}
    virg = a.get("virgula_decimal", False)
    if a.get("formato") == "csv":
        cols = linha.split(a.get("separador", ";"))
        for v in vs:
            i = int(v["ascii"])
            n = num(cols[i], virg) if i < len(cols) else None
            val[v["chave"]] = None if n is None else n * float(v.get("escala_ascii", 1))
    else:
        padrao = r"([A-Za-z_0-9][\w.]*)\s*[=:]\s*(-?\d+(?:,\d+)?)" if virg else r"([A-Za-z_0-9][\w.]*)\s*[=:]\s*(-?\d+(?:\.\d+)?)"
        kv = {k.upper(): x for k, x in re.findall(padrao, linha)}
        for v in vs:
            n = num(kv.get(str(v["ascii"]).upper()), virg)
            val[v["chave"]] = None if n is None else n * float(v.get("escala_ascii", 1))
    return val if any(x is not None for x in val.values()) else None


# ------------------------------------------------------------------- simulador
class Simulador:
    def __init__(self, cfg):
        self.cfg, self.t = cfg, 0.0

    def ler(self):
        self.t += self.cfg["intervalo_ms"] / 1000
        f = self.t % 40
        if f < 3: rpm = 1750 * f / 3
        elif f < 22: rpm = 1750 + 15 * math.sin(self.t)
        elif f < 25: rpm = 1750 * (25 - f) / 3
        else: rpm = 0
        mov, val = rpm > 5, {}
        for v in self.cfg["variaveis"]:
            k, x = v["chave"], 0.0
            if "veloc" in k: x = rpm / 17.5 if "pct" in k else rpm
            elif k == "corrente": x = 4.2 + rpm / 1750 * 3 + random.random() * .3 if mov else 0.05
            elif k == "frequencia": x = rpm / 29.17
            elif k == "torque": x = 55 + random.random() * 8 if mov else 0
            elif k == "vdc": x = 540 + random.random() * 6 + (60 if 22 < f < 25 else 0)
            elif k == "tensao_saida": x = rpm / 1750 * 380
            elif k == "temperatura": x = 38 + 6 * math.sin(self.t / 300) + (4 if mov else 0)
            elif k == "zsw": x = 0xEF37 if mov else 0xEB31
            elif v.get("falha"): x = 2 if random.random() < 0.002 else 0
            e = float(v.get("escala", 1)) or 1
            val[k] = round(x / e) * e
        return val


# ------------------------------------------------------- detecção de estado
class DetectorEstado:
    """Movimento quando |var_movimento| > limiar, mantido por 'confirmacao_s'."""

    def __init__(self, limiar, confirmacao_s):
        self.lim, self.deb = limiar, confirmacao_s
        self.estado, self.cand, self.cand_t = None, None, 0
        self.ciclos = []  # [estado, inicio, fim]

    def atualizar(self, t, valor):
        if valor is None:
            return self.estado
        cond = "movimento" if abs(valor) > self.lim else "parada"
        if self.estado is None:
            self._mudar(cond, t)
        elif cond != self.estado:
            if self.cand != cond:
                self.cand, self.cand_t = cond, t
            if t - self.cand_t >= self.deb:
                self._mudar(cond, self.cand_t)
        else:
            self.cand = None
        return self.estado

    def _mudar(self, novo, t):
        if self.ciclos:
            self.ciclos[-1][2] = t
        self.ciclos.append([novo, t, None])
        self.estado, self.cand = novo, None


# ---------------------------------------------------------------------- coleta
def coletar(args):
    cfg = carregar_config(args.config)
    if args.porta:
        cfg["porta"] = args.porta
    vs, modo = cfg["variaveis"], "sim" if args.simular else cfg["modo"]
    ser = fonte = None
    if modo != "sim":
        try:
            import serial
        except ImportError:
            sys.exit("Instale o pyserial: pip install -r requirements.txt")
        if not cfg.get("porta"):
            sys.exit("Informe a porta: --porta COM3 (Windows) ou /dev/ttyUSB0 (Linux)")
        ser = serial.Serial(cfg["porta"], int(cfg["baud"]), bytesize=int(cfg["bits_dados"]),
                            parity=cfg["paridade"][0].upper(), stopbits=int(cfg["stop_bits"]), timeout=0.02)
        fonte = ModbusRTU(ser, cfg["timeout_ms"] / 1000)
    else:
        fonte = Simulador(cfg)

    os.makedirs(args.saida, exist_ok=True)
    nome = os.path.join(args.saida, "dados_" + datetime.now().strftime("%Y%m%d_%H%M%S") + ".csv")
    det = DetectorEstado(float(cfg["limiar"]), float(cfg["confirmacao_s"]))
    vmov = cfg["var_movimento"]
    print(f"Modo: {modo} | gravando em {nome} | Ctrl+C para encerrar")
    inicio, n, ult_err, linha_buf, ult_falha = time.time(), 0, {}, b"", {}
    with open(nome, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["timestamp", "data_hora", "estado"] + [v["chave"] for v in vs])
        try:
            while not args.duracao or time.time() - inicio < args.duracao:
                t_ini = time.time()
                leituras = []
                if modo == "sim":
                    leituras.append(fonte.ler())
                    if args.rapido:  # simulador acelerado p/ testes
                        t_ini = inicio + n * cfg["intervalo_ms"] / 1000
                elif modo == "modbus":
                    val = {}
                    for v in vs:
                        try:
                            q = 2 if v["tipo"].endswith("32") else 1
                            val[v["chave"]] = decodificar(v, fonte.ler(int(cfg["escravo"]), int(v["fc"]), int(v["end"]), q))
                        except IOError as e:
                            val[v["chave"]] = None
                            if time.time() - ult_err.get(str(e), 0) > 10:
                                print("⚠", e); ult_err[str(e)] = time.time()
                        time.sleep(0.004)
                    leituras.append(val)
                else:  # ascii
                    linha_buf += ser.read(ser.in_waiting or 1)
                    while b"\n" in linha_buf:
                        l, linha_buf = linha_buf.split(b"\n", 1)
                        v = parse_linha(l.decode(errors="ignore").strip(), cfg)
                        if v: leituras.append(v)
                for val in leituras:
                    if not any(x is not None for x in val.values()):
                        continue
                    t = t_ini
                    ant = det.estado
                    est = det.atualizar(t, val.get(vmov))
                    if est != ant:
                        print(f"{datetime.fromtimestamp(t):%H:%M:%S}  {'▶ MOVIMENTO' if est == 'movimento' else '■ PARADA'}")
                    for v in vs:
                        if v.get("falha") and val.get(v["chave"]) and val[v["chave"]] != ult_falha.get(v["chave"]):
                            print(f"✖ {v['nome']}: código {val[v['chave']]:g}")
                        if v.get("falha") and val.get(v["chave"]) is not None:
                            ult_falha[v["chave"]] = val[v["chave"]]
                    w.writerow([f"{t:.3f}", datetime.fromtimestamp(t).strftime("%d/%m/%Y %H:%M:%S"), est or ""] +
                               ["" if val.get(v["chave"]) is None else f"{val[v['chave']]:.4f}".replace(".", ",") for v in vs])
                    n += 1
                if n % 20 == 0:
                    f.flush()
                if modo != "ascii" and not args.rapido:
                    time.sleep(max(0, cfg["intervalo_ms"] / 1000 - (time.time() - t_ini)))
                if args.rapido and n * cfg["intervalo_ms"] / 1000 >= (args.duracao or 600):
                    break
        except KeyboardInterrupt:
            print("\nColeta encerrada pelo usuário.")
    if ser:
        ser.close()
    print(f"{n} amostras gravadas em {nome}")
    if not args.sem_relatorio and n > 1:
        gerar_relatorio(nome, cfg, args.saida)


# ------------------------------------------------------------------- relatório
def ler_csv(caminho, vs):
    amostras = []
    with open(caminho, encoding="utf-8-sig") as f:
        r = csv.DictReader(f, delimiter=";")
        for row in r:
            val = {v["chave"]: (None if row.get(v["chave"], "") in ("", None) else float(row[v["chave"]].replace(",", "."))) for v in vs}
            amostras.append((float(row["timestamp"]), val))
    return amostras


def analisar(amostras, cfg):
    det = DetectorEstado(float(cfg["limiar"]), float(cfg["confirmacao_s"]))
    vmov = cfg["var_movimento"]
    for t, val in amostras:
        det.atualizar(t, val.get(vmov))
    ini, fim = amostras[0][0], amostras[-1][0]
    ciclos = [[e, a, b if b is not None else fim] for e, a, b in det.ciclos]

    def estado_em(t, idx=[0]):
        while idx[0] < len(ciclos) - 1 and t >= ciclos[idx[0] + 1][1]:
            idx[0] += 1
        return ciclos[idx[0]][0]

    estados = [estado_em(t) if ciclos else None for t, _ in amostras]
    tm = sum(b - a for e, a, b in ciclos if e == "movimento")
    tp = sum(b - a for e, a, b in ciclos if e == "parada")
    mov = [b - a for e, a, b in ciclos if e == "movimento"]
    par = [b - a for e, a, b in ciclos if e == "parada"]
    stats = {}
    for v in cfg["variaveis"]:
        s = {"movimento": [], "parada": []}
        for (t, val), e in zip(amostras, estados):
            if e and val.get(v["chave"]) is not None:
                s[e].append(val[v["chave"]])
        stats[v["chave"]] = {k: (min(x), sum(x) / len(x), max(x)) if x else None for k, x in s.items()}
    falhas = []
    for v in cfg["variaveis"]:
        if v.get("falha"):
            ant = 0
            for t, val in amostras:
                x = val.get(v["chave"])
                if x and x != ant:
                    falhas.append((t, v["nome"], x))
                if x is not None:
                    ant = x
    falhas.sort()
    return dict(ini=ini, fim=fim, tot=fim - ini, tm=tm, tp=tp, ciclos=ciclos, estados=estados, stats=stats, falhas=falhas,
                partidas=sum(1 for i, c in enumerate(ciclos) if i > 0 and c[0] == "movimento"),
                paradas=sum(1 for i, c in enumerate(ciclos) if i > 0 and c[0] == "parada"),
                m_mov=sum(mov) / len(mov) if mov else 0, m_par=sum(par) / len(par) if par else 0,
                max_par=max(par) if par else 0, util=tm / (tm + tp) * 100 if tm + tp else 0)


def dur(s):
    s = int(round(s)); h, m, ss = s // 3600, s % 3600 // 60, s % 60
    return (f"{h}h " if h else "") + (f"{m}min " if h or m else "") + f"{ss}s"


def dt(t):
    return datetime.fromtimestamp(t).strftime("%d/%m/%Y %H:%M:%S")


def br(x, casas=1):
    if x is None:
        return "—"
    return f"{x:,.{casas}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def casas(v):
    e = float(v.get("escala", 1)) or 1
    return 0 if e >= 1 else min(3, math.ceil(-math.log10(e)))


def grafico(amostras, r, cfg, caminho):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    vs = {v["chave"]: v for v in cfg["variaveis"]}
    vmov = cfg["var_movimento"]
    v2 = cfg.get("var_grafico_2") or ("corrente" if "corrente" in vs else None)
    passo = max(1, len(amostras) // 4000)
    am = amostras[::passo]
    ts = [datetime.fromtimestamp(t) for t, _ in am]
    fig, ax = plt.subplots(figsize=(10, 3.6), dpi=130)
    for e, a, b in r["ciclos"]:
        ax.axvspan(datetime.fromtimestamp(a), datetime.fromtimestamp(b), color="#1a9e55" if e == "movimento" else "#c9461d", alpha=.12, lw=0)
    ax.plot(ts, [v.get(vmov) for _, v in am], color="#1f6feb", lw=1.2)
    ax.set_ylabel(f"{vs[vmov]['nome']} ({vs[vmov].get('unidade', '')})", color="#1f6feb", fontsize=8)
    if v2 and v2 != vmov:
        ax2 = ax.twinx()
        ax2.plot(ts, [v.get(v2) for _, v in am], color="#d97706", lw=1)
        ax2.set_ylabel(f"{vs[v2]['nome']} ({vs[v2].get('unidade', '')})", color="#d97706", fontsize=8)
        ax2.tick_params(labelsize=7)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M:%S"))
    ax.tick_params(labelsize=7)
    ax.grid(alpha=.25)
    fig.tight_layout()
    fig.savefig(caminho)
    plt.close(fig)


def gerar_relatorio(csv_path, cfg, saida):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import cm
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image

    vs = cfg["variaveis"]
    amostras = ler_csv(csv_path, vs)
    if len(amostras) < 2:
        sys.exit("Poucos dados para gerar relatório.")
    r = analisar(amostras, cfg)
    base = os.path.join(saida, os.path.splitext(os.path.basename(csv_path))[0].replace("dados_", "relatorio_"))
    png = base + "_grafico.png"
    grafico(amostras, r, cfg, png)
    mq = cfg.get("maquina", {})
    vnome = {v["chave"]: v["nome"] for v in vs}

    # ---- PDF
    st = getSampleStyleSheet()
    H = lambda t: Paragraph(t, st["Heading2"])
    estilo = TableStyle([("GRID", (0, 0), (-1, -1), .4, colors.grey), ("FONTSIZE", (0, 0), (-1, -1), 8.5),
                         ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#eef1f4")), ("VALIGN", (0, 0), (-1, -1), "MIDDLE")])
    estilo_cab = TableStyle([("GRID", (0, 0), (-1, -1), .4, colors.grey), ("FONTSIZE", (0, 0), (-1, -1), 8.5),
                             ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eef1f4"))])
    el = [Paragraph("Relatório de Máquina — Movimento × Parada", st["Title"]),
          Paragraph(f"Emitido em {datetime.now():%d/%m/%Y %H:%M}", st["Normal"]), Spacer(1, 8), H("Identificação")]
    ident = [["Máquina", mq.get("nome", "—")], ["Drive / modelo", mq.get("drive", cfg.get("perfil_nome", "—"))],
             ["Responsável", mq.get("responsavel", "—")], ["Cliente / setor", mq.get("cliente", "—")],
             ["Período", f"{dt(r['ini'])} até {dt(r['fim'])} ({dur(r['tot'])})"],
             ["Critério de movimento", f"|{vnome[cfg['var_movimento']]}| > {br(float(cfg['limiar']), 2)}, confirmação {br(float(cfg['confirmacao_s']), 1)} s"],
             ["Arquivo de dados", os.path.basename(csv_path)]]
    t = Table(ident, colWidths=[5 * cm, 12 * cm]); t.setStyle(estilo); el += [t, H("Resumo operacional")]
    resumo = [["Tempo em movimento", f"{dur(r['tm'])} ({br(r['util'])} %)", "Tempo parada", f"{dur(r['tp'])} ({br(100 - r['util'])} %)"],
              ["Partidas", r["partidas"], "Paradas", r["paradas"]],
              ["Duração média movimento", dur(r["m_mov"]), "Duração média parada", dur(r["m_par"])],
              ["Maior parada", dur(r["max_par"]), "Falhas registradas", len(r["falhas"])]]
    t = Table(resumo, colWidths=[4.5 * cm, 4 * cm, 4.5 * cm, 4 * cm])
    t.setStyle(estilo); t.setStyle(TableStyle([("BACKGROUND", (2, 0), (2, -1), colors.HexColor("#eef1f4"))]))
    el += [t, H("Tendência"), Image(png, width=17 * cm, height=6.1 * cm),
           Paragraph("<font size=8>Faixa verde = movimento · faixa laranja = parada</font>", st["Normal"]), H("Grandezas por estado")]
    g = [["Variável", "Unid.", "Mov. mín", "Mov. média", "Mov. máx", "Par. mín", "Par. média", "Par. máx"]]
    for v in vs:
        if v.get("falha"):
            continue
        s, c = r["stats"][v["chave"]], casas(v)
        lin = [Paragraph(f"<font size=8>{v['nome']}</font>", st["Normal"]), v.get("unidade", "")]
        for k in ("movimento", "parada"):
            lin += [br(x, c) for x in s[k]] if s[k] else ["—"] * 3
        g.append(lin)
    t = Table(g, colWidths=[4.6 * cm, 1.6 * cm] + [1.8 * cm] * 6); t.setStyle(estilo_cab); el += [t, H("Falhas")]
    if r["falhas"]:
        t = Table([["Data/hora", "Variável", "Código"]] + [[dt(a), b, f"{c:g}"] for a, b, c in r["falhas"][:200]])
        t.setStyle(estilo_cab); el.append(t)
    else:
        el.append(Paragraph("Nenhuma falha registrada no período.", st["Normal"]))
    el.append(H("Ciclos" + (" (últimos 300)" if len(r["ciclos"]) > 300 else "")))
    cic = [["#", "Estado", "Início", "Fim", "Duração"]] + [[i + 1, e.capitalize(), dt(a), dt(b), dur(b - a)] for i, (e, a, b) in enumerate(r["ciclos"])][-300:]
    t = Table(cic, repeatRows=1); t.setStyle(estilo_cab); el.append(t)
    if mq.get("observacoes"):
        el += [H("Observações"), Paragraph(mq["observacoes"], st["Normal"])]
    SimpleDocTemplate(base + ".pdf", pagesize=A4, leftMargin=2 * cm, rightMargin=2 * cm, topMargin=1.5 * cm, bottomMargin=1.5 * cm,
                      title="Relatório de Máquina").build(el)

    # ---- Excel
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    wb = Workbook()
    cab = lambda ws: [setattr(c, "font", Font(bold=True)) or setattr(c, "fill", PatternFill("solid", fgColor="EEF1F4")) for c in ws[1]]
    ws = wb.active; ws.title = "Resumo"
    for a, b in ident: ws.append([a, b])
    ws.append([])
    for lin in resumo: ws.append([lin[0], lin[1]]); ws.append([lin[2], lin[3]])
    ws.append(["Tempo em movimento (s)", round(r["tm"], 1)]); ws.append(["Tempo parada (s)", round(r["tp"], 1)])
    ws.append(["Utilização (%)", round(r["util"], 2)])
    for c in ws["A"]: c.font = Font(bold=True)
    ws.column_dimensions["A"].width = 28; ws.column_dimensions["B"].width = 50
    ws = wb.create_sheet("Grandezas")
    ws.append(["Variável", "Unidade", "Mov. mín", "Mov. média", "Mov. máx", "Par. mín", "Par. média", "Par. máx"]); cab(ws)
    for v in vs:
        if v.get("falha"): continue
        s = r["stats"][v["chave"]]
        ws.append([v["nome"], v.get("unidade", "")] + sum(([round(x, 4) for x in s[k]] if s[k] else [None] * 3 for k in ("movimento", "parada")), []))
    ws.column_dimensions["A"].width = 34
    ws = wb.create_sheet("Ciclos")
    ws.append(["#", "Estado", "Início", "Fim", "Duração (s)"]); cab(ws)
    for i, (e, a, b) in enumerate(r["ciclos"]):
        ws.append([i + 1, e, datetime.fromtimestamp(a), datetime.fromtimestamp(b), round(b - a, 1)])
    for col in "CD":
        ws.column_dimensions[col].width = 20
        for c in ws[col][1:]: c.number_format = "dd/mm/yyyy hh:mm:ss"
    ws = wb.create_sheet("Falhas")
    ws.append(["Data/hora", "Variável", "Código"]); cab(ws)
    for a, b, c in r["falhas"]: ws.append([datetime.fromtimestamp(a), b, c])
    ws = wb.create_sheet("Dados")
    ws.append(["Data/hora", "Estado"] + [f"{v['nome']} ({v.get('unidade', '')})" for v in vs]); cab(ws)
    for (t, val), e in list(zip(amostras, r["estados"]))[:1_000_000]:
        ws.append([datetime.fromtimestamp(t), e] + [val.get(v["chave"]) for v in vs])
    for c in ws["A"][1:]: c.number_format = "dd/mm/yyyy hh:mm:ss"
    ws.column_dimensions["A"].width = 20
    ws.freeze_panes = "A2"
    wb.save(base + ".xlsx")
    print(f"Relatório: {base}.pdf\nPlanilha:  {base}.xlsx")


# ------------------------------------------------------------------------- CLI
def main():
    ap = argparse.ArgumentParser(description="Leitor serial de drives + relatório movimento/parada")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("perfis", help="lista perfis de drive disponíveis")
    c = sub.add_parser("coletar", help="coleta dados da serial")
    c.add_argument("-c", "--config", default=os.path.join(AQUI, "config_exemplo.json"))
    c.add_argument("--porta", help="COM3, /dev/ttyUSB0 ...")
    c.add_argument("--simular", action="store_true", help="usa dados simulados (sem hardware)")
    c.add_argument("--rapido", action="store_true", help="simulador acelerado (teste)")
    c.add_argument("--duracao", type=float, default=0, help="segundos (0 = até Ctrl+C)")
    c.add_argument("--saida", default="saida")
    c.add_argument("--sem-relatorio", action="store_true")
    r = sub.add_parser("relatorio", help="gera PDF + Excel a partir de um CSV coletado")
    r.add_argument("csv")
    r.add_argument("-c", "--config", default=os.path.join(AQUI, "config_exemplo.json"))
    r.add_argument("--saida", default="saida")
    a = ap.parse_args()
    if a.cmd == "perfis":
        for k, p in carregar_perfis().items():
            print(f"{k:20s} {p['nome']}")
    elif a.cmd == "coletar":
        coletar(a)
    else:
        os.makedirs(a.saida, exist_ok=True)
        gerar_relatorio(a.csv, carregar_config(a.config), a.saida)


if __name__ == "__main__":
    main()
