# Leitor Serial de Drives — Relatório de Máquina (Movimento × Parada)

Duas versões, mesma lógica e mesmos perfis (`perfis.json`):

| | **web/leitor_drives.html** | **python/leitor_drive.py** |
|---|---|---|
| Uso | Diagnóstico rápido, na hora | Coleta longa (horas/dias), sem supervisão |
| Requisito | Chrome ou Edge no PC (Web Serial) | Python 3.9+ e `pip install -r requirements.txt` |
| Saídas | PDF (imprimir → Salvar como PDF), CSV de dados, CSV de ciclos | CSV gravado continuamente + PDF + Excel (.xlsx) |

## Hardware
- Conversor USB-RS485 (2 fios A/B + GND) ligado à porta Modbus do drive. Para RS-232, cabo direto.
- No drive: habilite o Modbus RTU e defina endereço, baud, paridade e stop bits iguais aos do app.
  - WEG CFW500/CFW11: P0308 (endereço), P0310 (baud), P0311 (paridade/stop bits). Confirme no manual de comunicação do seu modelo.
  - Siemens V20: macro de conexão Cn010 (Modbus), P2021 (endereço), P2010 (baud).
  - ABB ACS355/ACS550: grupo 53 (protocolo EFB / Modbus embutido); a seleção do protocolo muda conforme o modelo.
- Termine a linha RS-485 (120 Ω) em cabos longos.

## Modos de leitura
1. **Modbus RTU** — o app pergunta ao drive (funções 03/04) a cada intervalo. 16 ou 32 bits, com ou sem sinal, com escala.
2. **Texto ASCII** — o drive (ou CLP/placa) envia linhas prontas:
   - Chave=valor: `RPM=1500;I=7.2;F=50.0` (o campo "ascii" da variável é a chave)
   - CSV: `1500;7.2;50.0` (o campo "ascii" é o índice da coluna, começando em 0)
   - No modo ASCII o valor é usado como recebido. Use `escala_ascii` no JSON para converter se for preciso.
3. **Simulador** — dados fictícios para testar o app e o relatório sem hardware.

## Detecção de movimento × parada
- Movimento quando |variável de referência| > limiar (ex.: |velocidade| > 10 rpm).
- A mudança de estado só vale depois que a condição se mantém pelo **tempo de confirmação** (padrão 2 s). Assim, reversões e oscilações não viram paradas falsas.
- O relatório traz: tempo em movimento/parado, % de utilização, nº de partidas e paradas, durações médias, maior parada, mín/média/máx de cada grandeza **separada por estado**, falhas com data/hora e a lista de ciclos.

## Perfis incluídos (endereços base 0; registrador 4xxxx = endereço + 40001)
- **Genérico** — editável, para qualquer drive.
- **WEG CFW500 / CFW11** — endereço = nº do parâmetro (P0002 velocidade, P0003 corrente, P0004 Vcc, P0005 Hz, P0007 V saída, P0009 torque, P0030 temperatura, P0048 alarme, P0049 falha).
- **WEG SCA06** — base equivalente; confirme as escalas.
- **Siemens V20 / G120** — ZSW (40110) e HIW (40111, 16384 = 100 %). Acrescente os parâmetros que o seu firmware mapeia.
- **ABB ACS355 / ACS550** — 4xxxx = 40000 + parâmetro (01.02 velocidade, 01.04 corrente, 04.01 última falha…).

> ⚠ Os mapas foram montados a partir da documentação pública e servem como ponto de partida. Endereços e escalas mudam conforme o modelo e o firmware: **confirme sempre no manual de comunicação do drive** antes de usar os números num relatório oficial.

## Versão web — passo a passo
1. Abra `web/leitor_drives.html` no Chrome ou Edge (duplo clique, não precisa de servidor).
2. Aba **Conexão**: escolha o modo, o perfil e os parâmetros seriais → **Conectar e iniciar** → selecione a porta COM.
3. Aba **Variáveis**: ajuste endereços e escalas. Use **Exportar configuração** para guardar o mapa de cada máquina.
4. Aba **Monitor**: valores ao vivo, tendência com faixas de estado e eventos.
5. Aba **Relatório**: preencha a identificação → **Gerar PDF** / **Exportar CSV**.

## Versão Python — passo a passo
```bash
pip install -r requirements.txt
python leitor_drive.py perfis                                   # lista perfis
python leitor_drive.py coletar --simular --duracao 120          # teste sem hardware
python leitor_drive.py coletar -c config_exemplo.json --porta COM3          # Windows
python leitor_drive.py coletar -c config_exemplo.json --porta /dev/ttyUSB0  # Linux
python leitor_drive.py relatorio saida/dados_AAAAMMDD_HHMMSS.csv -c config_exemplo.json
```
- Ctrl+C encerra a coleta e gera o relatório automaticamente (pasta `saida/`).
- O CSV é gravado durante a coleta, então uma queda de energia não perde os dados.
- O relatório recalcula os estados a partir dos dados brutos. Dá para mudar `limiar` ou `confirmacao_s` no config e gerar de novo.
- Mapa próprio: copie a lista `variaveis` de um perfil para dentro do seu config e edite.
