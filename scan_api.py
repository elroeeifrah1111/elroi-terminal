"""Scanner subsystem — ported from trading-alerts into Elroi Terminal.

Self-contained: multi-symbol background scans (Python / Pine / strategy.*),
saved + scheduled scans with Telegram alerts on new matches, ticker lists,
single-symbol Pine/Python sandbox endpoints.

Mount from server.py:
    import scan_api
    scan_api.set_candle_loader(load_candles)   # fn(symbol, period, interval) -> {"candles": [...]}
    app.include_router(scan_api.router)
    scan_api.start_scan_runner()               # daemon thread, scheduled scans
"""

import gc
import json
import logging
import multiprocessing
import os
import pickle
import re
import sqlite3
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from io import StringIO
from typing import Dict, List, Optional, Tuple

import pandas as pd
import requests
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from scanner import (
    PINE_SCAN_NOTE,
    PYTHON_EXAMPLE,
    PY_INDICATOR_EXAMPLE,
    PY_SRFLIP_INDICATOR_EXAMPLE,
    PY_SRFLIP_SCAN_EXAMPLE,
    PY_BASEBO_INDICATOR_EXAMPLE,
    PY_BASEBO_SCAN_EXAMPLE,
    PY_STRATEGY_EXAMPLE,
    MAX_SYMBOLS_INTRADAY,
    batch_load_candles,
    run_python_indicator,
    run_python_strategy,
    run_scan,
)
from pine_engine import PineError, run_pine
from strategy_engine import (
    OPTIMIZE_METRICS,
    list_strategies,
    optimize_strategy,
    run_backtest,
)


MAX_SYMBOLS = 500


logger = logging.getLogger("scan_api")

router = APIRouter()

# ----------------------------------------------------------------------------
# Candle loader (injected by server.py to avoid a circular import)
# ----------------------------------------------------------------------------
_candle_loader = None


def set_candle_loader(fn):
    global _candle_loader
    _candle_loader = fn


def _load_candles(symbol: str, period: str, interval: str) -> dict:
    if _candle_loader is None:
        raise RuntimeError("scan_api candle loader not set")
    return _candle_loader(symbol, period, interval)


def _clean_symbol(s: str) -> str:
    return re.sub(r"[^A-Z0-9.\-]", "", (s or "").upper().strip())


# ----------------------------------------------------------------------------
# Telegram (optional; scans notify here on new matches)
# ----------------------------------------------------------------------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()


def escape_md(text: str) -> str:
    return re.sub(r"([_*\[\]()~`>#+\-=|{}.!])", r"\\\1", str(text))


def send_telegram_message(text: str, chat_id: Optional[str] = None):
    target_chat = chat_id or TELEGRAM_CHAT_ID
    if not TELEGRAM_BOT_TOKEN or not target_chat:
        logger.warning("[Telegram Not Configured] Message: %s", text[:120])
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": target_chat, "text": text, "parse_mode": "Markdown"}
    try:
        resp = requests.post(url, json=payload, timeout=8)
        if resp.status_code != 200:
            logger.warning("Telegram API returned %s: %s", resp.status_code,
                           resp.text[:200])
    except Exception as err:
        logger.error("Failed to send Telegram message: %s", err)


# ----------------------------------------------------------------------------
# SQLite — saved scans only
# ----------------------------------------------------------------------------
_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scans.db")
_db_lock = threading.RLock()
_db_conn: Optional[sqlite3.Connection] = None


def _get_db() -> sqlite3.Connection:
    global _db_conn
    if _db_conn is None:
        _db_conn = sqlite3.connect(_DB_PATH, check_same_thread=False)
        _db_conn.row_factory = sqlite3.Row
        with _db_lock:
            _db_conn.execute("PRAGMA journal_mode=WAL")
            _db_conn.execute(
                """
                CREATE TABLE IF NOT EXISTS scans (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    source_type TEXT NOT NULL DEFAULT 'preset',
                    source_id TEXT NOT NULL DEFAULT '',
                    symbols_json TEXT NOT NULL DEFAULT '[]',
                    interval TEXT NOT NULL DEFAULT '1d',
                    period TEXT NOT NULL DEFAULT '1y',
                    language TEXT NOT NULL DEFAULT 'python',
                    code TEXT NOT NULL DEFAULT '',
                    schedule_minutes INTEGER NOT NULL DEFAULT 0,
                    active INTEGER NOT NULL DEFAULT 1,
                    last_run REAL NOT NULL DEFAULT 0,
                    last_matches_json TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL
                )
                """
            )
            _db_conn.commit()
    return _db_conn


def _row_to_scan(row: sqlite3.Row) -> Dict:
    return {
        "id": row["id"], "name": row["name"],
        "source": {"type": row["source_type"], "id": row["source_id"],
                   "symbols": json.loads(row["symbols_json"] or "[]")},
        "interval": row["interval"], "period": row["period"],
        "language": row["language"], "code": row["code"],
        "schedule_minutes": row["schedule_minutes"],
        "active": bool(row["active"]), "last_run": row["last_run"],
        "last_matches": json.loads(row["last_matches_json"] or "[]"),
        "created_at": row["created_at"],
    }


def db_list_scans() -> List[Dict]:
    conn = _get_db()
    with _db_lock:
        rows = conn.execute("SELECT * FROM scans ORDER BY id DESC").fetchall()
    return [_row_to_scan(r) for r in rows]


def db_get_scan(scan_id: int) -> Optional[Dict]:
    conn = _get_db()
    with _db_lock:
        row = conn.execute("SELECT * FROM scans WHERE id = ?", (scan_id,)).fetchone()
    return _row_to_scan(row) if row else None


def db_save_scan(data: Dict) -> Dict:
    conn = _get_db()
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    with _db_lock:
        cur = conn.execute(
            """INSERT INTO scans (name, source_type, source_id, symbols_json,
                   interval, period, language, code, schedule_minutes, active,
                   last_run, last_matches_json, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (data["name"], data["source"]["type"], str(data["source"].get("id") or ""),
             json.dumps(data["source"].get("symbols") or []),
             data["interval"], data["period"], data["language"], data["code"],
             data["schedule_minutes"], 1, 0, "[]", now),
        )
        conn.commit()
        scan_id = cur.lastrowid
    return db_get_scan(scan_id)


def db_update_scan(scan_id: int, data: Dict) -> Optional[Dict]:
    conn = _get_db()
    with _db_lock:
        cur = conn.execute(
            """UPDATE scans SET name=?, source_type=?, source_id=?, symbols_json=?,
                   interval=?, period=?, language=?, code=?, schedule_minutes=?
               WHERE id=?""",
            (data["name"], data["source"]["type"], str(data["source"].get("id") or ""),
             json.dumps(data["source"].get("symbols") or []),
             data["interval"], data["period"], data["language"], data["code"],
             data["schedule_minutes"], scan_id),
        )
        conn.commit()
        if cur.rowcount == 0:
            return None
    return db_get_scan(scan_id)


def db_delete_scan(scan_id: int) -> bool:
    conn = _get_db()
    with _db_lock:
        cur = conn.execute("DELETE FROM scans WHERE id = ?", (scan_id,))
        conn.commit()
    return cur.rowcount > 0


def db_scan_touch(scan_id: int, matches: List[str]) -> None:
    conn = _get_db()
    with _db_lock:
        conn.execute(
            "UPDATE scans SET last_run = ?, last_matches_json = ? WHERE id = ?",
            (time.time(), json.dumps(matches or []), scan_id),
        )
        conn.commit()

# ----------------------------------------------------------------------------
# Ticker lists
# ----------------------------------------------------------------------------
TICKER_LIST_DEFS = {
    "all_us": {"label": "All US Stocks", "label_he": "כל ארה״ב"},
    "sp500": {"label": "S&P 500", "label_he": "S&P 500"},
    "nasdaq100": {"label": "NASDAQ 100", "label_he": "נסדאק 100"},
    "russell2000": {"label": "Russell 2000 (IWM)", "label_he": "ראסל 2000 (IWM)"},
}

_RUSSELL2000_SNAPSHOT = (
    "FLWS ONEM DIBS SRCE TSVT TWOU SCWO DDD FDMT ETNB EGHT MASS ATEN AIR AAN ANF ABM ABSI ACTG ASO "
    "ACAD AKR ACEL ACCO ACCD ARAY SLRN ACHV ACIW ACMR ACNB ACRV ATNM ABOS GOLF ACVA AHCO ADPT ADCT ADUS "
    "ADEA ADNT ADMA CVSA ADTH ADTN ARQ AEIS ASIX ADV ADVM SYRE AEHR AMTX AERI AJRD AVTE AVAV ASLE AEVA "
    "AFCG AFMD AGEN AGTI AGL AGYS AGIO MITT AIRS ATSG AKBA AKRO AKYA ALG ALRM AIN ALBO ALDX ALEC ALRS "
    "ALEX ALX ALCO ALIT ALHC ALKT ALKS ABTX ALGT ALE AMOT ALLO AOSL AMR ATEC ALPN PINE ALTG ALTR ALT "
    "ANRO AIMC AMPS ALMS ALTI ALXO AMAL AMRK OSG AMBA AMC AMCX AMTB AMRC AAT DCH AEO AEL AHR AMNB "
    "APEI ARL LGTY AWR AMSC AVD AMWD CRMT AMSF ABCB ATLO FOLD POWW AMRX AMN AMPH AMPY AMPL AMRSQ ANAB "
    "AVXL ANDE AOMR ANGO ANIK ANIP ANNX ATEX HOUS AIV APOG APGE ARI ASTH GBTG APPHQ APPN APLE APLD AIT "
    "AAOI APLT AQST ABR ABUS ALTM ARCB ACLX LFG ACHR AROC PTRAQ ARNC ACA ARCT RCUS ARQT AMBP ARDX ARDT "
    "ASC ACRE AGX ARGO ARHS ARIS ARKO ARLO AHH ARR ARRY AVBP AROW ARWR AIP APAM ARTV AORT ARVN ASAN "
    "ABG EFOR ASPN UP ASPI AMK ASB ASTE ASTR ATXS ATRO ASTS ASUR ATRA AVIR ATKR AUB ATLC AAWW AESI "
    "ATCX ATMU ATNI ATOS ATRC ATRI AUDA AEYE AURA AUPH AUR AVDL AVNS AVTA AVYA AVAH AVPT AVNW CDMO RNAM "
    "AVID AVDX AVNT AVA OABI RCEL ACLS AXGN AXNX AX AXSM AZZ BBLNF BLZE BMI BCPC BALY BANF BANC TBBK "
    "BAND BSVN BFC BOH BMRC NTB BPRN BCAL BKU BWFG BANR BHB BARK B BBSI BCML BCBP BODY BECN BEAM "
    "SKIN BZH BBBYQ BDC BELFB BELFA BHE BNFT BHIL BGRY BBT BRY VWESQ BYND BGC BGCP BGS BCAX BBAI CMRC "
    "BH BIOA BCRX BHVN BLFS BMEA BNGO BTMD BVS BRDS BTBT BJRI BKV BLKB BDTX BKH BL BKSY BXMT BLND "
    "BLNK BE BLMN BLUE BLBD BLFY BVH BXC BPMC BCC BOOT BORR BOC BOW BWMN BOXDQ BOX BHR BDN BRZE "
    "BRCC BFH BBIO BWB MNRL BHG AAMI BRSP BTSG BV RILY EAT BCO VTOL BRMK BNL BKD BBUC BRKL BWIN "
    "BRT BTRS BKE BBW BMBL BUR BHRB BFST BY BYRN AISP AI CCCC CABA CABO CBT WHD CADE CDZI CDRE "
    "CSTE CVGW CMCL CAL CRC CWT CALX CPE CALM CATC CAC CWH CADL CNNE CANO GOEV CTLP CBNK CCBG CFFN "
    "CAPR CSTR CRDF CSII CDLX CDNA CMAXQ CTRE CRGX CARG CRBU CRS CSV CARS CARE CASA CWST FLNA CASS CSTL "
    "GYRE CPRX CATY CVCO CBZ CBL CECO FUN CELC CLDX CENN CSR CENT CENTA CPF LEU CTRI CENX CCS IPSC "
    "CRNC CERE CBLL CERS CEVA CGON CHX ECOM CRGEQ CHPT GTLS CCF CLDT CAKE CHEF CHGG CCXI CHMG CPK REFI "
    "CHS CIM KDNY COFS NAGE CHUY CBUS CMPR CINC CNK CIFR CIR CTRN CZFS CZNC CHCO CIO CIVB CMTG CLAR "
    "CLNE CLSK CCO CLFD YOU CWAN CLW CLMB CLPR CCNE CNO CNX CCB CDXS CDE COGT CCOI CNS CHRS COHU "
    "COLL CBAN CLBK CMCO CMC CVGI CBU CHCT CYH CTBI CWBC CVLT CODI COMP CMP CMPX CMPO CIX CRK CON "
    "BBCP CNDT CNMD CNOB CONN CCSI CWCO CSTM ROAD CPSS TCS CTGO WISH CTNM CPS CRBP CORT CXW HJ1 CMT "
    "CORZ CORZQ CRMD CDP CRSR CRVL CMRE BASE COUR CVLG CVET COWN PMTS CBRL CRAI CRD-A CRDO CRGY CRCT CRNX "
    "CRML CCRN CFB CYRX TYDE LAW CSGS CSWI CTIC CTO CTS HLTHQ CGEM CURB CURO CWK CUBI CVBF CVI CVRX "
    "CYBE CTKB CYTK CYXTQ DJCO DC DAKT DAN DSKE PLAY DAVE DCT DCPH DH DK DLX DNLI DEN DENN DBI "
    "DSGN DM DESP DXLG DHT DHIL DO DRH DICE DBD DBDQQ CRVO DGII DMRC DBRG DOCN APPS DCOM DIN DIOD "
    "DSGR DSEY DHC DLHC BOOM DCGO DOLE DOMA DOMO DGICA DFIN LPG DORM DAWN PLOW DEI CVT DFH DRQ INVX "
    "DRVN DRS DCO QBTS DXPE DY DVAX DYN DX ETWO EGBN EGLE ESTE DEA EBC EML KODK EBIXQ ECHO ECVT "
    "EPC EWTX EGIO EDIT EGAN EIGR ELEV CLYM EFC ELME LOCO EMBC EEX EP ESRT EIG ACT ENTA ECPG EU "
    "WIRE ENR UUUU ERII NRGV EPAC ENS NETI ENFN ESMT EHAB ELVN EBF RENB ENVA ENVX NPO ENSG ESGR EBTC "
    "EFSC TRDA EVC ENV PLUS EQRX ETRN EQBK ERAS ESCA ESE ESPR ESQ ESSA ESNT EPRT ETD EWCZ EVEX EB "
    "EVBG EVCM EVRI EVER MRAM EVTC EVGO EVI EVH EOLS EPM EVLV EVOP AQUA SSP EE EXLS EXPO EXPR XPRO "
    "AGNT EXTR EYPT FN FMAO FMNB FPI FARO FSLY FATE FATH FBK AGM FSS FENC FBLG PLGO FDBC FIGS FISI "
    "FA FBP FNLC FBNC FBMS FRBA BUSE FBIZ FCFS FCF FCBC FFBC FFIN THFF FFNW FFWM INBK FIBK FRME FMBH "
    "FLIC FWRG MYFW NOTE FSRNQ FSBC FBC SOC FLNG FLXS FLNC FLR FFIC FLYW FOCS FHTX FL AFRI BLX FOR "
    "FRGE FORG FMTX FORM FORR FTAI FET FWRD FCPT FOXF GUTS FRG FBRT FC FELE FSP KRRO DMC FRSH T2T "
    "FTDR ULCC FRPH FSBW FIP FUBO FCEL FULC FLGT FLL FULT FNKO FF FVCB GALT GRSD GCI GATX GCMG GCTS "
    "GLSHQ GENC GNK WGS GBIO GCO GNE THRM GNW GEO GEOS GABC GERN GETY GTY ROCK GCT GIII GBCI GOOD "
    "LAND GLT GKOS GBT GIC GMRE GNL GSAT GWRS GMS GOGO GOCO GLNG GDEN GMGI GOGLT GT GSHD GPRO GRC "
    "EAF GHM GHC GVA GPMT GRNT LENZ GTN RPT GLDD GSBC GRBK GBX GDOT GCBC GRNA GLRE GPRE GLSI GEF "
    "GEF-B GDYN GFF GRND GPI GRPN GRWG GNTY GH GRDN GPOR HCKT HAE HAIN HNRG HALO HBB HG HLNE HWC "
    "HBI HNGR HAFC HASI HONE HLIT HRMY HROW HSC HBIO HVT HE HA HWKN HAYN FUL HBT HCI HCSG HCAT "
    "HQY DOC OBIO HSTM HTLD HTLF HL HEES HSII HELE HLGN HLIO HOS HP HLF HRI HTBK HCCI HFWA HRTG "
    "HRTX HT HTZ HSKA HFFG HIBB HPK HI HLVX HLMN HTH HGV HSHP HIMS HIFS HIPO HQI HRT HNI HLLY "
    "HBCP HOMB HMPT HMST HTBI HNST HOFT HOPE HMN SEAT HBNC TWNK HOV HUBG HPP HDSN HUMA HURN V71 HYLN "
    "HY IIIV IAUX IBEX IBTA ICFI ICHR ICVX ICUI IDYA IDT IESC IGMS IHRT WULF IMGO IMAX IMMR IBRX IMGN "
    "IMNM IMVT PI NARI IRT INDB IBCP INDT ILPT INFN III IEA INFU NGVT IMKTA INBX INMD INMB INOD IOSP "
    "INNV IIPR CTV INVA INGN INO INZY INSG NSIT INSM NSP INSE IBP IIIN INST INTA ITGR IAS IART NTLA "
    "ICPT IDCC TILE IBOC BRSL IMXI INSW IPAR IPI LUNR IVT IVR ISTR ITIC NVTAQ IVVD IONQ IOVA IRMD IRTC "
    "IRBT IRNT IRON IRWD CATX ISPR SAFE ITOS ITRI IE ISEE JACK JXN JAKK JRVR JAMF JBI JANX JSPR JBGS "
    "JELD JBLU JILL JJSF JOAN JOBY JBTM JBSS JMSB JOUT WLY JYNT JNCE KAI KALU KLR KLTR KALV KAMN KRAT "
    "KRTX KBH KRNY KELYA KMT KW KROS KFRC KE KBAL KLC KNTK KWY KNSA KNTE KRG KREF KNBE KN KGS "
    "KOD KTB KOP KFY KOS KTOS KNF DNUT KRO KRYS KLIC KURA KRUS KYMR KYTX LADR LBAI LKFN LSEA LE "
    "LNTH LNZA LRMR LTCH SWIM LAUR LZB TGLS FSTR LCII LCNB LEGH LZ LMAT LMND LC TREE LESL LXEO LXRX "
    "LGIH LHCG LBRT LILA LILAK BATRK BATRA LFCR LFMD LFST LCUT LTH LWAY LZM LGND ZEV LWLG LMB LMNR LINC "
    "LIND LNN LCTX LNKB LGF-A LGF-B LQDA LQDT LIVN LTHM LOB LVO RAMP LVOX P9N LLFLQ RIDE LOVE LYLTQ LXU "
    "LYTS LTC LUMN LAZR LBC LXFR LXP LYEL LYRA MCBC MAC MGNX MSGE MDGL DNTH MGNI MGY MHLD MBUU MAMA "
    "TUSK MTW MN MNKD MARA MRVI MCS MMI HZO MPX MKTW MQ MRTN MZTI DOOR MBC MCFT MTRN MATV MTRX "
    "MATX MTTR MATW MLP MAXR MXCT MMS MXL MEC MBI MBX MGRC MDC MFIN MAX MED MDWD MGTX MBWM MBIN "
    "MCY MRCY VIVO MLNK MTH MMSI MRSN MLAB MGX MTAL MEI MCBS MCB MFA MGEE MGPI STRC MVIS MBCN MSEX "
    "MSBI MPB MOFG MHO MLR MLKN MDXG MNMD MTX MLYS MIR MIRM AVO MCW MG MITK MODN MOD MODVQ MC "
    "MNTV MCRI MGI ML MNRO MNTK GLUE ONT MOG-A MORF MOV MRC COOP MLI MWA MUR MVBF MYE MYRG MYGN "
    "NABL NBR NC NNE NSTGQ NNOX NSSC NATH NBHC NKSH FIZZ NCMI NHC NHI NPK NRC EYE NWLI NGS NGVC "
    "NATR NAUT NAVI NVTS NBBK NBTB NATL VYX RTL NKTR NNI NGMS NEOG NEO NGNE NRDS NRDY CTOS NTGR NLOP "
    "NPWR NTCT NTST NMRA NPCE NVRO NJR NMRK NPKI NEWT FLG ADAM NXDT NREF NXRT NEXT NXDR NXGN NEX NN "
    "NXT NGM NIC NODK NKLA NKTX LASR NL NMIH NNBR NE NAT NBN NECB NOG NTIC NFBK NRIM NWBI NWE "
    "NWN NWPX NWFL NG NOVT NVAX NVCR DNOW NRIX SMR NUS NUVL NUVA NUVB NVEE NVEC OVLY OII OCFC OCGN "
    "OCUL ONIT ODP OPAD OFG OI ODC OIS OLPX ONB OSBC OLMA OLO ZEUS OFLX OMER OMCL ONTF ONCT OGS "
    "STKS OLP OSPN OSW ONEW OOMA OPEN KAR LPRO OPK OPFI OPRX OPCH OBT OSUR ORC ORGO ORIC OBNK OEC "
    "ORN ONL ORA ORRF OFIX KIDS OSCR OSIS OTTR OUST OB OTLK BBBY OVID ACH OXM RPC PACB PPBI PCRX "
    "PACS PTVE PGY PD PAGS PLMR PAMT PANL PZZA FNA PGRE PRDS PKE TRAK PKBK PRK PKOH PARR PAR PRTYQ "
    "CASH PAX PATK PDCO PTEN PAYA PAYO PSFE PAYS PBF PCB CNXN PCSB PDFS PDLI BTU PKST PGC PEARQ PEB "
    "MD PTON PNTG PFSI PMT PEBO PEBK PFIS PEPG PRDO PWP PRFT PHLT PRM PESI PPTA WOOF PETQ PFSW PGTI "
    "PHAT CELL PAHC PECO PHIN PLAB PHR PLL PDM PING PBFS PIPR PBI PJT PL AGS MYPS PLXS PLRX PLUG "
    "PLBC PLYM TXNM PNT PLM PDLB PRCH PTLO POR POSH PSTL PBPB PCH POWL AIOT POWI PWSC PRAA PROP PRAX "
    "PGEN PFBC PLPC PRLD PFC PBH PSMT PNRG PRME FRST PRIM PRMW PRMB PRTH PRVA PRA PRCT PFHD PAL ACDC "
    "PRG PRGS PGNY PROK RXDX PUMP PRO PTGX PRTA PRLB PRVB PVBC PFS PTCT PUBM LUNG PLSE PBYI PCYO PCT "
    "PRPL PYXS PZN QTWO QTTB QCRH QUAD KWR QLYS NX QTRX QSI QRHC QUIK QNST QIPT QUOT RCM RXT RDN "
    "RLGT RADI RDNT METCB METC RMBS RNGR ROCC PACK RPD RAPP RAPT RYAM RBB RICK RC REAX REAL RETA RXRX "
    "RDFN RRBI RRR RDVT RDW RWT RGNX RM RGLS REKR RLAY RMAX RELY RNST RPAY REPL RBCAA FRBKQ RSVR REZI "
    "RFP RGP ROIC RVNC REVG RVMD RVLV REX RGCO RYTM RBBN RELL RIGL REPX RMNI REI BIOP RADCQ WEST RLJ "
    "RMR DTI RKLB RCKT RKLYQ RCKY ROG ROOT ROVR RES RDNW RUSHB RUSHA RSI RUTH RXO RXST RYZ RHP SBRA "
    "SABR SB SAFT SAGE SBH SANA SMTI SD SASR SANM SPNS BFS SVRA SVV SCSC RDUS SRRK SCHL SDGR SNCE "
    "SCLX STNG SCPH SCU SBCF SMHI SDRL SPNE PRKS 1S70 SIGI SEM SLQT WTTR SMLR SEMR SMTC SENEA SXT SEPN "
    "SERA SVC SFBS SES SEVN SEZL SFL SHAK SHCR STTK SHEN SHLS SWAV SHOE SHBI SSTK SHYF SIBN BSRR SIGA "
    "SGHT SIG SLAB SILK SVCO SPRY SBOW SAMG SI SFNC SMPL SLP SBGI SPNT SITC SITM STR HTO SKIL SKYE "
    "SKYH SKY SKWD SKYT SKYW SNBRQ SLG SMBK PENG SMRT SM SMID SWBI SNPO SEI SMXT SWI SLNO SLDB SLDP "
    "DTCB SLGC SAH SNDA SONO SRNE SOUN SSTI SFST SMBC SSBK SJI SLND SPFI SBSI SSB SWX SOVO SPTN SPIR "
    "SR SPOK SP SWTX CXM SFM SPT SPSC SPXC SQSP SSRM STAA STGW LAB SMP SXI STHO STRYQ STBA SCS "
    "STEL STEM SCL STEP STXS SBT STER STRL SHOO STC SFIX JOE SYBT STOK STNE SRI SNEX SRTA STRA STRS "
    "STRW LRN RGR SMMF INN SUM SMMT SUMO SXC SNCY SUNL NOVAQ STKL SPWRQ RUN SHO SGHC SGC SMCI RGTI "
    "SUPN SGRY SRDX STRO SG SWKH SLVM SYNA SNDX SST TCMD TALS TRML TALK TALO TNDM SKT TNGX TH TARS "
    "TTCFQ TAYD TMHC TSHA TTGT TK TNK TGNA TRC TDOC TDS TELL TELO TLS TENB TNYA TNC TEX TERN LLAP "
    "TRNO TTI TVGN TCBI TGH TGTX TBPH THR THRX TCBX THRD THRN TDUP THRY INDI TDW TTSH TLYS TSBK MTUS "
    "TIPT TWI TITN TMP CALY CURV TOWN TSQ TRTX TPICQ COOK TRNS TCI TMDX RIG TGAN TA TVTX TMCI TIG "
    "TG THS TRVI TCDAQ TCBK TRS TNET TRN TPH TRTN TFIN TGI TROX TBI TRUE TRUP TRST TRMK TEN TCRX "
    "TTEC TTMI TCX TUP TPB TBCH TPC TWIN TWST TWO TYRA UDMY UFPI UFPT UCTT ULBI UMBF UMH UNF UIS "
    "UBSI UCB UFCS UHG ACIC UNFI USLM UNIT UTL UNTY UVV UHT UVE ULH USAP UTI UVSP UPBD UPST UPB "
    "UPWK UEC UE URBN URG URGN UBA USNA USCB USER USPH SLCA UTMD UTZ VVX EGY VCSA RDZN VAL VHI "
    "VLY VALU VNDA VREX VRNS PCVX VBIV VGR VECO VLD VEL VLDR VTYX VRA VCYT VSTM VERA VGAS VCEL VRNT "
    "VRE VBTX VRTV VRRM VRCA VTNR VERX VERU VERV PRSU DSP VIA VSAT VIAV VICR VSCO VCTR VMD VIEW VRAY "
    "VLGEA VMEO BBIG VIR VIRC SPCE VABK VRDN VTSI VRTS VSH VPG VISN VSTO VC COCO VTLE VITL VTS VVNT "
    "VZIO VLTA VYGR VSEC WNC WALD WD WRBY HCC WAFD WASH WSBF WTS WVE WAY WDFC WEAV WEBR WBTN WMK "
    "WEJOF HOWL WERN WSBC WABC WTBA WEYS WSR FREE WOW WLDN WLFC WINA WGO WT KLG MAPS WNS WWW WKHS "
    "WK WRLD WKC WOR WS WSFS WTI XFOR XBIT XNCR XHR XERS XRX XOMA XMTR XOS XPEL XPER XPOF YELP "
    "YEXT YMAB YORW ZNTL ZETA ZVRA ZD ZIMV ZIP ZUMZ ZUO ZURA ZWS ZYME ZYXI "
)

_TICKER_LIST_UA = {"User-Agent": "Mozilla/5.0 (compatible; elroi-terminal/1.0)"}
_ticker_lists_cache: Dict[str, Tuple[float, List[str]]] = {}
TICKER_LIST_TTL = 6 * 3600


def _clean_ticker_list(raw: List[str]) -> List[str]:
    out, seen = [], set()
    for t in raw:
        s = (t or "").strip().upper().replace(".", "-")
        if not s or s in seen:
            continue
        if not re.fullmatch(r"[A-Z0-9\-]{1,12}", s):
            continue
        seen.add(s)
        out.append(s)
    return out


def _fetch_text_tickers(url: str) -> List[str]:
    r = requests.get(url, headers=_TICKER_LIST_UA, timeout=30)
    r.raise_for_status()
    return [ln.strip() for ln in r.text.splitlines() if ln.strip()]


def _fetch_ticker_list(list_id: str) -> List[str]:
    now = time.time()
    hit = _ticker_lists_cache.get(list_id)
    if hit and now - hit[0] < TICKER_LIST_TTL:
        return hit[1]
    symbols: List[str] = []
    try:
        if list_id == "all_us":
            tickers = _fetch_text_tickers(
                "https://raw.githubusercontent.com/rreichel3/US-Stock-Symbols/main/all/all_tickers.txt")
            symbols = _clean_ticker_list(tickers)
        elif list_id == "russell2000":
            # Bundled snapshot 2026-09-11 (upstream source file removed) —
            # Russell 2000 rebalances annually; refresh snapshot when needed.
            symbols = _clean_ticker_list(_RUSSELL2000_SNAPSHOT)
        elif list_id == "sp500":
            html = requests.get("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
                                headers=_TICKER_LIST_UA, timeout=30).text
            tables = pd.read_html(StringIO(html))
            symbols = _clean_ticker_list(tables[0]["Symbol"].astype(str).tolist())
        elif list_id == "nasdaq100":
            html = requests.get("https://en.wikipedia.org/wiki/List_of_NASDAQ-100_companies",
                                headers=_TICKER_LIST_UA, timeout=30).text
            tables = pd.read_html(StringIO(html))
            symbols = _clean_ticker_list(tables[0]["Ticker"].astype(str).tolist())
    except Exception as e:
        logger.warning("ticker list %s fetch failed (%s)", list_id, e)
    if symbols:
        _ticker_lists_cache[list_id] = (now, symbols)
    return symbols


# ----------------------------------------------------------------------------
# Candle loading — bulk yfinance in an isolated child + per-symbol fallback
# ----------------------------------------------------------------------------
def _bulk_download_child_main(result_path: str, symbols: List[str], period: str,
                               interval: str) -> None:
    """Child-process entry: run the yfinance bulk download in isolation.

    The child must never touch file descriptors inherited from the request
    worker — it closes every FD >= 3 first, reports back only through a
    result file, and leaves via os._exit() to skip cleanup of inherited
    objects.
    """
    try:
        os.closerange(3, 65536)
    except Exception:
        pass
    try:
        try:
            _devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(_devnull, 1)
            os.dup2(_devnull, 2)
        except Exception:
            pass
        data = batch_load_candles(symbols, period, interval)
        payload = {"ok": True, "data": data}
    except Exception as e:  # noqa: BLE001 — report, don't die silent
        payload = {"ok": False, "error": str(e)[:200]}
    try:
        with open(result_path, "wb") as f:
            pickle.dump(payload, f)
    except Exception:
        pass
    os._exit(0)


_BULK_CHILD_TIMEOUT = 60  # seconds; then fall back to per-symbol


def _bulk_candles_isolated(symbols: List[str], period: str,
                           interval: str) -> Dict[str, List[Dict]]:
    try:
                # forkserver: see scanner.py — safe fork from threaded server process.
        ctx = multiprocessing.get_context("forkserver")
    except (ValueError, AttributeError):
        return {}
    fd, result_path = tempfile.mkstemp(prefix="bulk_scan_")
    os.close(fd)
    p = None
    try:
        p = ctx.Process(target=_bulk_download_child_main,
                        args=(result_path, list(symbols), period, interval))
        p.start()
        p.join(_BULK_CHILD_TIMEOUT)
        if p.is_alive():
            logger.warning("bulk download child hung (%d symbols) — terminating",
                           len(symbols))
            p.terminate()
            p.join(10)
            if p.is_alive():
                p.kill()
                p.join(10)
            return {}
        try:
            with open(result_path, "rb") as f:
                payload = pickle.load(f)
        except Exception:
            logger.warning("bulk download child died without result "
                           "(%d symbols, exit=%s)", len(symbols), p.exitcode)
            return {}
        if isinstance(payload, dict) and payload.get("ok") \
                and isinstance(payload.get("data"), dict):
            return payload["data"]
        logger.warning("bulk download child reported: %s", payload)
        return {}
    except Exception as e:
        logger.warning("isolated bulk download failed: %s", e)
        return {}
    finally:
        if p is not None:
            try:
                p.close()
            except Exception:
                pass
        try:
            os.unlink(result_path)
        except Exception:
            pass


def _scan_candles(symbols: List[str], period: str,
                  interval: str) -> Tuple[Dict[str, List[Dict]], List[str]]:
    """Bulk candles (isolated child) + reliable per-symbol fallback for misses.
    Memory guard: the fallback runs INSIDE the parent, so it is only used for
    a small number of missing symbols."""
    candles = _bulk_candles_isolated(symbols, period, interval)
    missing = [s for s in symbols if s not in candles]
    if missing and len(missing) <= 25:
        def _one(sym: str):
            try:
                d = _load_candles(sym, period, interval)
                return sym, d.get("candles") or []
            except Exception:
                return sym, []
        with ThreadPoolExecutor(max_workers=3) as ex:
            for sym, cl in ex.map(_one, missing):
                if cl:
                    candles[sym] = cl
        missing = [s for s in symbols if s not in candles]
    elif missing:
        logger.warning("bulk download failed for %d/%d symbols — skipping "
                       "in-parent fallback to protect memory",
                       len(missing), len(symbols))
    return candles, missing


def _malloc_trim() -> None:
    try:
        import ctypes
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except Exception:
        pass


def _run_scan_job(symbols: List[str], interval: str, period: str,
                  language: str, code: str) -> Dict:
    """Memory-safe: symbols are processed in chunks so we never hold millions
    of candle dicts at once (512MB instance). Results are merged at the end."""
    t0 = time.time()
    intraday = interval not in ("1d", "1wk")
    chunk_size = 150 if intraday else 500
    all_results: List[Dict] = []
    missing: List[str] = []
    total_with_data = 0
    for i in range(0, len(symbols), chunk_size):
        chunk = symbols[i:i + chunk_size]
        candles_by_symbol, chunk_missing = _scan_candles(chunk, period, interval)
        total_with_data += len(candles_by_symbol)
        missing.extend(chunk_missing)
        chunk_results = run_scan(code, candles_by_symbol, language=language,
                                 interval=interval)
        all_results.extend(chunk_results)
        del candles_by_symbol, chunk_results, chunk
        gc.collect()
        _malloc_trim()
    matched = sorted([r for r in all_results if r.get("signal")],
                     key=lambda r: (-r.get("score", 0), r["symbol"]))
    rest = sorted([r for r in all_results if not r.get("signal")],
                  key=lambda r: r["symbol"])
    ordered = matched + rest
    return {
        "results": [
            {k: r.get(k) for k in ("symbol", "price", "change_pct", "signal",
                                   "score", "note", "error")}
            for r in ordered
        ],
        "scanned": total_with_data,
        "matched": len(matched),
        "matched_symbols": [r["symbol"] for r in matched],
        "missing": missing,
        "elapsed_seconds": round(time.time() - t0, 1),
    }

# ----------------------------------------------------------------------------
# Request models
# ----------------------------------------------------------------------------
class ScanSourceModel(BaseModel):
    type: str = "preset"   # preset | custom
    id: str = ""
    symbols: List[str] = []


class ScanRunRequest(BaseModel):
    source: ScanSourceModel
    interval: str = "1d"
    period: Optional[str] = None
    language: str = "python"
    code: str = ""


class ScanSaveRequest(BaseModel):
    name: str
    source: ScanSourceModel
    interval: str = "1d"
    period: Optional[str] = None
    language: str = "python"
    code: str = ""
    schedule_minutes: int = 0


class PineRunRequest(BaseModel):
    symbol: str = "AAPL"
    period: str = "1mo"
    interval: str = "1d"
    code: str = ""


class BacktestRequest(BaseModel):
    symbol: str = "AAPL"
    period: str = "1mo"
    interval: str = "1d"
    strategy_id: str = "ema_cross"
    params: Optional[Dict[str, float]] = None
    initial_capital: float = 10000.0
    commission_pct: float = 0.1


class OptimizeRequest(BaseModel):
    symbol: str = "AAPL"
    period: str = "1mo"
    interval: str = "1d"
    strategy_id: str = "ema_cross"
    metric: str = "profit_factor"
    params: Optional[Dict[str, float]] = None


class PyChartRequest(BaseModel):
    symbol: str = "AAPL"
    period: str = "1mo"
    interval: str = "1d"
    code: str = ""
    initial_capital: float = 10000.0
    commission_pct: float = 0.1


SCAN_INTERVAL_PERIOD = {
    "5m": "5d", "15m": "1mo", "30m": "1mo", "1h": "1mo",
    "1d": "1y", "1wk": "max",
}


# ----------------------------------------------------------------------------
# Endpoints
# ----------------------------------------------------------------------------
@router.get("/api/strategies")
def api_list_strategies():
    return {"strategies": list_strategies(),
            "metrics": list(OPTIMIZE_METRICS.keys())}


@router.post("/api/py/indicator")
def api_py_indicator(req: PyChartRequest):
    code = (req.code or "").strip()
    if not code:
        raise HTTPException(status_code=400, detail="אין קוד להרצה")
    sym = _clean_symbol(req.symbol)
    data = _load_candles(sym, req.period, req.interval)
    try:
        result = run_python_indicator(code, data["candles"], symbol=sym)
    except Exception as e:
        logger.exception("py indicator failed")
        raise HTTPException(status_code=400, detail=str(e))
    if result.get("error"):
        raise HTTPException(status_code=400, detail=result["error"])
    result.update({
        "title": "Python indicator",
        "kind": "indicator",
        "symbol": sym,
        "period": req.period,
        "interval": req.interval,
        "num_bars": len(data["candles"]),
    })
    return result


@router.post("/api/py/backtest")
def api_py_backtest(req: PyChartRequest):
    code = (req.code or "").strip()
    if not code:
        raise HTTPException(status_code=400, detail="אין קוד להרצה")
    sym = _clean_symbol(req.symbol)
    data = _load_candles(sym, req.period, req.interval)
    try:
        result = run_python_strategy(code, data["candles"], symbol=sym,
                                     initial_capital=req.initial_capital,
                                     commission_pct=req.commission_pct)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("py backtest failed")
        raise HTTPException(status_code=400, detail=str(e))
    result["symbol"] = sym
    result["period"] = req.period
    result["interval"] = req.interval
    return result


@router.post("/api/backtest")
def api_backtest(req: BacktestRequest):
    sym = _clean_symbol(req.symbol)
    data = _load_candles(sym, req.period, req.interval)
    try:
        result = run_backtest(
            data["candles"], req.strategy_id, req.params,
            initial_capital=req.initial_capital,
            commission_pct=req.commission_pct)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    result["symbol"] = sym
    result["period"] = req.period
    result["interval"] = req.interval
    return result


@router.post("/api/optimize")
def api_optimize(req: OptimizeRequest):
    sym = _clean_symbol(req.symbol)
    data = _load_candles(sym, req.period, req.interval)
    try:
        result = optimize_strategy(
            data["candles"], req.strategy_id, metric=req.metric,
            params=req.params)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    result["symbol"] = sym
    result["period"] = req.period
    result["interval"] = req.interval
    return result


@router.post("/api/pine/run")
def api_pine_run(req: PineRunRequest):
    code = (req.code or "").strip()
    if not code:
        raise HTTPException(status_code=400, detail="אין קוד להרצה")
    sym = _clean_symbol(req.symbol)
    data = _load_candles(sym, req.period, req.interval)
    try:
        result = run_pine(data["candles"], code, symbol=sym,
                          period=req.period, interval=req.interval)
    except PineError as e:
        raise HTTPException(status_code=400, detail=str(e))
    result["symbol"] = sym
    result["period"] = req.period
    result["interval"] = req.interval
    return result


def _resolve_scan_symbols(source: Dict) -> List[str]:
    stype = (source.get("type") or "preset").lower()
    if stype == "preset":
        lid = (source.get("id") or "").strip()
        if lid not in TICKER_LIST_DEFS:
            raise HTTPException(status_code=400, detail="רשימה לא נמצאה")
        return _fetch_ticker_list(lid)
    if stype == "custom":
        raw = source.get("symbols") or []
        if isinstance(raw, str):
            raw = re.split(r"[\s,;]+", raw)
        return _clean_ticker_list([str(s) for s in raw])
    raise HTTPException(status_code=400, detail="סוג מקור לא נתמך")


@router.post("/api/scan/run")
def api_scan_run(req: ScanRunRequest):
    code = (req.code or "").strip()
    if not code:
        raise HTTPException(status_code=400, detail="אין קוד סריקה")
    language = (req.language or "python").lower()
    if language not in ("python", "pine"):
        raise HTTPException(status_code=400, detail="שפה לא נתמכת (python/pine)")
    interval = req.interval if req.interval in SCAN_INTERVAL_PERIOD else "1d"
    period = req.period or SCAN_INTERVAL_PERIOD[interval]
    symbols = _resolve_scan_symbols(req.source.dict())
    symbols = [s for s in dict.fromkeys(symbols) if s]
    if not symbols:
        raise HTTPException(status_code=400, detail="הרשימה ריקה — אין מה לסרוק")
    if len(symbols) > MAX_SYMBOLS:
        raise HTTPException(
            status_code=400,
            detail=f"רשימת הסמלים גדולה מדי ({len(symbols)}). מקסימום {MAX_SYMBOLS} סמלים לסריקה. בחר רשימה קטנה יותר או הזן סמלים מותאמים."
        )
    if interval not in ("1d", "1wk") and len(symbols) > MAX_SYMBOLS_INTRADAY:
        raise HTTPException(
            status_code=400,
            detail=f"סריקת אינטרוול תוך-יומי מוגבלת ל-{MAX_SYMBOLS_INTRADAY} סימולים (נבחרו {len(symbols)})",
        )
    try:
        return _run_scan_job(symbols, interval, period, language, code)
    except Exception as e:
        logger.exception("scan failed")
        raise HTTPException(status_code=500, detail=f"הסריקה נכשלה: {e}")


@router.get("/api/scan/meta")
def api_scan_meta():
    return {
        "python_example": PYTHON_EXAMPLE,
        "pine_note": PINE_SCAN_NOTE,
        "py_indicator_example": PY_INDICATOR_EXAMPLE,
        "py_strategy_example": PY_STRATEGY_EXAMPLE,
        "py_srflip_indicator_example": PY_SRFLIP_INDICATOR_EXAMPLE,
        "py_srflip_scan_example": PY_SRFLIP_SCAN_EXAMPLE,
        "py_basebo_indicator_example": PY_BASEBO_INDICATOR_EXAMPLE,
        "py_basebo_scan_example": PY_BASEBO_SCAN_EXAMPLE,
        "max_symbols_intraday": MAX_SYMBOLS_INTRADAY,
        "intervals": list(SCAN_INTERVAL_PERIOD.keys()),
    }


@router.get("/api/ticker-lists")
def api_ticker_lists():
    return [
        {"id": lid, "label": d["label"], "label_he": d["label_he"],
         "count": len(_fetch_ticker_list(lid))}
        for lid, d in TICKER_LIST_DEFS.items()
    ]


@router.get("/api/ticker-lists/{list_id}")
def api_ticker_list(list_id: str):
    if list_id not in TICKER_LIST_DEFS:
        raise HTTPException(status_code=404, detail="רשימה לא נמצאה")
    symbols = _fetch_ticker_list(list_id)
    if not symbols:
        raise HTTPException(status_code=502,
                            detail="טעינת הרשימה נכשלה — נסה שוב מאוחר יותר")
    d = TICKER_LIST_DEFS[list_id]
    return {"id": list_id, "label": d["label"], "symbols": symbols}


def _strip_scan_code(s: Dict) -> Dict:
    d = dict(s)
    d["code"] = (d.get("code") or "")[:0]  # list view: no code payload
    d["has_code"] = True
    return d


def _scan_save_data(req: ScanSaveRequest) -> Dict:
    name = (req.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="נדרש שם לסריקה")
    code = (req.code or "").strip()
    if not code:
        raise HTTPException(status_code=400, detail="אין קוד סריקה")
    language = (req.language or "python").lower()
    if language not in ("python", "pine"):
        raise HTTPException(status_code=400, detail="שפה לא נתמכת")
    interval = req.interval if req.interval in SCAN_INTERVAL_PERIOD else "1d"
    return {
        "name": name,
        "source": {"type": req.source.type, "id": req.source.id,
                   "symbols": req.source.symbols or []},
        "interval": interval,
        "period": req.period or SCAN_INTERVAL_PERIOD[interval],
        "language": language, "code": code,
        "schedule_minutes": max(0, int(req.schedule_minutes or 0)),
    }


@router.get("/api/scans")
def api_list_scans():
    return [_strip_scan_code(s) for s in db_list_scans()]


@router.post("/api/scans")
def api_save_scan(req: ScanSaveRequest):
    return db_save_scan(_scan_save_data(req))


@router.get("/api/scans/{scan_id}")
def api_get_scan(scan_id: int):
    s = db_get_scan(scan_id)
    if not s:
        raise HTTPException(status_code=404, detail="סריקה לא נמצאה")
    return s


@router.put("/api/scans/{scan_id}")
def api_update_scan(scan_id: int, req: ScanSaveRequest):
    s = db_update_scan(scan_id, _scan_save_data(req))
    if not s:
        raise HTTPException(status_code=404, detail="סריקה לא נמצאה")
    return s


@router.delete("/api/scans/{scan_id}")
def api_delete_scan(scan_id: int):
    if not db_delete_scan(scan_id):
        raise HTTPException(status_code=404, detail="סריקה לא נמצאה")
    return {"status": "deleted"}


@router.post("/api/scans/{scan_id}/toggle")
def api_toggle_scan(scan_id: int):
    s = db_get_scan(scan_id)
    if not s:
        raise HTTPException(status_code=404, detail="סריקה לא נמצאה")
    conn = _get_db()
    with _db_lock:
        conn.execute("UPDATE scans SET active = ? WHERE id = ?",
                     (0 if s["active"] else 1, scan_id))
        conn.commit()
    return {"status": "ok", "active": not s["active"]}


@router.post("/api/scans/{scan_id}/run")
def api_run_saved_scan(scan_id: int):
    s = db_get_scan(scan_id)
    if not s:
        raise HTTPException(status_code=404, detail="סריקה לא נמצאה")
    symbols = _resolve_scan_symbols(s["source"])
    symbols = [x for x in dict.fromkeys(symbols) if x]
    if not symbols:
        raise HTTPException(status_code=400, detail="הרשימה ריקה")
    try:
        out = _run_scan_job(symbols, s["interval"], s["period"],
                            s["language"], s["code"])
    except Exception as e:
        logger.exception("saved scan run failed")
        raise HTTPException(status_code=500, detail=f"הסריקה נכשלה: {e}")
    db_scan_touch(scan_id, out["matched_symbols"])
    return out


# ----------------------------------------------------------------------------
# Scheduled scans — daemon thread, Telegram on NEW matches only
# ----------------------------------------------------------------------------
def _execute_scheduled_scan(s: Dict) -> None:
    first_run = not s["last_run"]
    try:
        symbols = _resolve_scan_symbols(s["source"])
        symbols = [x for x in dict.fromkeys(symbols) if x]
        if not symbols:
            logger.warning("scheduled scan #%d: empty symbol list", s["id"])
            db_scan_touch(s["id"], [])
            return
        if s["interval"] not in ("1d", "1wk") and len(symbols) > MAX_SYMBOLS_INTRADAY:
            logger.warning("scheduled scan #%d: too many symbols for intraday (%d)",
                           s["id"], len(symbols))
            return
        out = _run_scan_job(symbols, s["interval"], s["period"],
                            s["language"], s["code"])
    except Exception:
        logger.exception("scheduled scan #%d failed", s["id"])
        return
    matched = out.get("matched_symbols") or []
    db_scan_touch(s["id"], matched)
    if first_run:
        logger.info("scheduled scan #%d first run: %d matches (no notify)",
                    s["id"], len(matched))
        return
    prev = set(s.get("last_matches") or [])
    new = [m for m in matched if m not in prev]
    if new and TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        by_sym = {r["symbol"]: r for r in out.get("results", [])}
        lines = []
        for sym in new[:15]:
            r = by_sym.get(sym, {})
            price = r.get("price")
            chg = r.get("change_pct", 0) or 0
            note = (r.get("note") or "").strip()
            price_s = f"${price:.2f}" if isinstance(price, (int, float)) else "—"
            line = f"• *{escape_md(sym)}* — {price_s} ({chg:+.1f}%)"
            if note:
                line += f"\n  {escape_md(note)}"
            lines.append(line)
        more = f"\n_ועוד {len(new) - 15}…_" if len(new) > 15 else ""
        msg = (f"🔍 *סריקה: {escape_md(s['name'])}*\n"
               f"נמצאו {len(new)} סימולים חדשים תואמים:\n\n"
               + "\n".join(lines) + more)
        send_telegram_message(msg)
        logger.info("scheduled scan #%d: notified %d new matches",
                    s["id"], len(new))


def _background_scan_runner():
    logger.info("Background scan runner started.")
    while True:
        try:
            time.sleep(30)
            now = time.time()
            for s in db_list_scans():
                try:
                    if not s["active"] or s["schedule_minutes"] <= 0:
                        continue
                    if now - (s["last_run"] or 0) < s["schedule_minutes"] * 60:
                        continue
                    logger.info("Running scheduled scan #%d (%s)", s["id"], s["name"])
                    _execute_scheduled_scan(s)
                except Exception:
                    logger.exception("error in scheduled scan #%d", s.get("id"))
        except Exception as err:
            logger.error("Error in scan runner: %s", err)


_scan_runner_started = False


def start_scan_runner():
    global _scan_runner_started
    if _scan_runner_started:
        return
    _scan_runner_started = True
    _get_db()  # ensure schema exists before serving
    t = threading.Thread(target=_background_scan_runner, daemon=True)
    t.start()
