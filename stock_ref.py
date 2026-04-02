"""
stock_ref.py — HK Stock Reference Database
============================================
Single source of truth for stock codes, names and industry groupings.
Codes are official HKEX 5-digit codes (identical across HKEX, etnet, CCASS).

Structure:
  STOCKS = {
      "00700": {
          "en":       "TENCENT",           # HKEX English name
          "zh":       "騰訊控股",            # etnet verified Chinese name
          "industry": "TEC",               # etnet industry code
          "ind_zh":   "科技",               # etnet industry Chinese label
          "type":     "bluechip",          # signal threshold bucket
      },
      ...
  }

Industry codes (etnet nature= parameter):
  ETF  ETF基金      BNK  銀行        INS  保險
  TEC  科技         SNS  軟件服務    AUT  汽車
  ENG  能源         UTL  公用事業    REP  地產
  HCR  醫療         IND  工業        MET  金屬礦產
  TEL  電訊         TRN  運輸        RET  零售消費
  CGM  綜合企業     FIN  金融        GEN  其他

Type buckets (for signal thresholds):
  etf      — normal short 40–70%
  stable   — normal short  5–10%  (banks, utilities, energy)
  bluechip — normal short 10–20%  (large-cap tech, insurance, transport)
  general  — normal short 10–25%  (everything else)
"""

STOCKS: dict[str, dict] = {

    # ── ETF ──────────────────────────────────────────────────────────────────
    "02800": {"en":"TRACKER FUND",          "zh":"盈富基金",          "industry":"ETF","ind_zh":"ETF",      "type":"etf"},
    "02828": {"en":"HSCEI ETF",             "zh":"恒生中國企業ETF",   "industry":"ETF","ind_zh":"ETF",      "type":"etf"},
    "03033": {"en":"CSOP HS TECH",          "zh":"南方恒生科技ETF",   "industry":"ETF","ind_zh":"ETF",      "type":"etf"},
    "03032": {"en":"PREMIA CHINA ETF",      "zh":"Premia中國新經濟ETF","industry":"ETF","ind_zh":"ETF",    "type":"etf"},
    "03188": {"en":"CSOP A50 ETF",          "zh":"華夏滬深三百ETF",   "industry":"ETF","ind_zh":"ETF",      "type":"etf"},
    "02846": {"en":"ISHARES HS TECH",       "zh":"iShares恒生科技ETF","industry":"ETF","ind_zh":"ETF",      "type":"etf"},
    "03140": {"en":"FUTURE HSI ETF",        "zh":"未來恒生指數ETF",   "industry":"ETF","ind_zh":"ETF",      "type":"etf"},
    "03037": {"en":"HUAXIA HS TECH ETF",    "zh":"華夏恒生科技ETF",   "industry":"ETF","ind_zh":"ETF",      "type":"etf"},
    "03011": {"en":"CSOP HSI ETF",          "zh":"南方恒生指數ETF",   "industry":"ETF","ind_zh":"ETF",      "type":"etf"},
    "02823": {"en":"ISHARES A50",           "zh":"iShares A50 ETF",   "industry":"ETF","ind_zh":"ETF",      "type":"etf"},

    # ── 銀行 ─────────────────────────────────────────────────────────────────
    "00005": {"en":"HSBC HOLDINGS",         "zh":"滙豐控股",          "industry":"BNK","ind_zh":"銀行",     "type":"stable"},
    "00011": {"en":"HANG SENG BANK",        "zh":"恒生銀行",          "industry":"BNK","ind_zh":"銀行",     "type":"stable"},
    "00023": {"en":"BANK OF E ASIA",        "zh":"東亞銀行",          "industry":"BNK","ind_zh":"銀行",     "type":"stable"},
    "00388": {"en":"HKEX",                  "zh":"香港交易所",        "industry":"FIN","ind_zh":"金融",     "type":"bluechip"},
    "00939": {"en":"CCB",                   "zh":"建設銀行",          "industry":"BNK","ind_zh":"銀行",     "type":"stable"},
    "01398": {"en":"ICBC",                  "zh":"工商銀行",          "industry":"BNK","ind_zh":"銀行",     "type":"stable"},
    "01288": {"en":"ABC",                   "zh":"農業銀行",          "industry":"BNK","ind_zh":"銀行",     "type":"stable"},
    "02388": {"en":"BOC HK",                "zh":"中銀香港",          "industry":"BNK","ind_zh":"銀行",     "type":"stable"},
    "03328": {"en":"BOCOM",                 "zh":"交通銀行",          "industry":"BNK","ind_zh":"銀行",     "type":"stable"},
    "03988": {"en":"BANK OF CHINA",         "zh":"中國銀行",          "industry":"BNK","ind_zh":"銀行",     "type":"stable"},

    # ── 保險 ─────────────────────────────────────────────────────────────────
    "01299": {"en":"AIA",                   "zh":"友邦保險",          "industry":"INS","ind_zh":"保險",     "type":"bluechip"},
    "02318": {"en":"PING AN",               "zh":"中國平安",          "industry":"INS","ind_zh":"保險",     "type":"bluechip"},
    "02628": {"en":"CHINA LIFE",            "zh":"中國人壽",          "industry":"INS","ind_zh":"保險",     "type":"bluechip"},
    "02328": {"en":"PICC P&C",              "zh":"中國財險",          "industry":"INS","ind_zh":"保險",     "type":"bluechip"},
    "00945": {"en":"MANULIFE",              "zh":"宏利金融",          "industry":"INS","ind_zh":"保險",     "type":"bluechip"},
    "06161": {"en":"CHINA TAIPING",         "zh":"中國太平",          "industry":"INS","ind_zh":"保險",     "type":"bluechip"},
    "02378": {"en":"PRUDENTIAL",            "zh":"保誠",              "industry":"INS","ind_zh":"保險",     "type":"bluechip"},

    # ── 科技平台 ─────────────────────────────────────────────────────────────
    "00700": {"en":"TENCENT",               "zh":"騰訊控股",          "industry":"TEC","ind_zh":"科技",     "type":"bluechip"},
    "09988": {"en":"BABA-W",                "zh":"阿里巴巴",          "industry":"TEC","ind_zh":"科技",     "type":"bluechip"},
    "00981": {"en":"SMIC",                   "zh":"中芯國際",          "industry":"TEC","ind_zh":"科技",     "type":"general"},
    "01810": {"en":"XIAOMI-W",              "zh":"小米集團",          "industry":"TEC","ind_zh":"科技",     "type":"bluechip"},
    "09618": {"en":"JD-SW",                 "zh":"京東集團",          "industry":"TEC","ind_zh":"科技",     "type":"bluechip"},
    "09888": {"en":"BAIDU-SW",              "zh":"百度集團",          "industry":"TEC","ind_zh":"科技",     "type":"bluechip"},
    "09999": {"en":"NETEASE-S",             "zh":"網易",              "industry":"TEC","ind_zh":"科技",     "type":"bluechip"},
    "03690": {"en":"MEITUAN-W",             "zh":"美團",              "industry":"TEC","ind_zh":"科技",     "type":"bluechip"},
    "09626": {"en":"BILIBILI-SW",           "zh":"嗶哩嗶哩",          "industry":"TEC","ind_zh":"科技",     "type":"bluechip"},
    "00020": {"en":"SENSETIME-W",           "zh":"商湯科技",          "industry":"TEC","ind_zh":"科技",     "type":"general"},
    "02382": {"en":"SUNNY OPT.",            "zh":"舜宇光學科技",      "industry":"TEC","ind_zh":"科技",     "type":"bluechip"},
    "03750": {"en":"CATL",                  "zh":"寧德時代",          "industry":"TEC","ind_zh":"科技",     "type":"bluechip"},

    # ── 軟件服務 ─────────────────────────────────────────────────────────────
    "00241": {"en":"ALI HEALTH",            "zh":"阿里健康",          "industry":"SNS","ind_zh":"軟件服務", "type":"bluechip"},
    "06618": {"en":"JD HEALTH",             "zh":"京東健康",          "industry":"SNS","ind_zh":"軟件服務", "type":"bluechip"},
    "00354": {"en":"CHINASOFT INT'L",       "zh":"中軟國際",          "industry":"SNS","ind_zh":"軟件服務", "type":"general"},
    "00992": {"en":"LENOVO",                "zh":"聯想集團",          "industry":"SNS","ind_zh":"軟件服務", "type":"bluechip"},

    # ── 汽車 ─────────────────────────────────────────────────────────────────
    "01211": {"en":"BYD",                   "zh":"比亞迪股份",        "industry":"AUT","ind_zh":"汽車",     "type":"bluechip"},
    "00175": {"en":"GEELY AUTO",            "zh":"吉利汽車",          "industry":"AUT","ind_zh":"汽車",     "type":"bluechip"},
    "02015": {"en":"LI AUTO-W",             "zh":"理想汽車",          "industry":"AUT","ind_zh":"汽車",     "type":"bluechip"},
    "09868": {"en":"XPENG-W",               "zh":"小鵬汽車",          "industry":"AUT","ind_zh":"汽車",     "type":"bluechip"},
    "02238": {"en":"GAC GROUP",             "zh":"廣汽集團",          "industry":"AUT","ind_zh":"汽車",     "type":"general"},

    # ── 能源 ─────────────────────────────────────────────────────────────────
    "00883": {"en":"CNOOC",                 "zh":"中國海洋石油",      "industry":"ENG","ind_zh":"能源",     "type":"stable"},
    "00386": {"en":"SINOPEC CORP",          "zh":"中國石化",          "industry":"ENG","ind_zh":"能源",     "type":"stable"},
    "00857": {"en":"PETROCHINA",            "zh":"中國石油股份",      "industry":"ENG","ind_zh":"能源",     "type":"stable"},
    "00135": {"en":"KUNLUN ENERGY",         "zh":"崑崙能源",          "industry":"ENG","ind_zh":"能源",     "type":"stable"},
    "00384": {"en":"CHINA GAS HOLD",        "zh":"中國燃氣",          "industry":"ENG","ind_zh":"能源",     "type":"stable"},
    "01193": {"en":"CR GAS",                "zh":"華潤燃氣",          "industry":"ENG","ind_zh":"能源",     "type":"stable"},
    "02688": {"en":"ENN ENERGY",            "zh":"新奧能源",          "industry":"ENG","ind_zh":"能源",     "type":"stable"},
    "00968": {"en":"XINYI SOLAR",           "zh":"信義光能",          "industry":"ENG","ind_zh":"能源",     "type":"general"},

    # ── 公用事業 ─────────────────────────────────────────────────────────────
    "00002": {"en":"CLP HOLDINGS",          "zh":"中電控股",          "industry":"UTL","ind_zh":"公用事業", "type":"stable"},
    "00003": {"en":"HK & CHINA GAS",        "zh":"香港中華煤氣",      "industry":"UTL","ind_zh":"公用事業", "type":"stable"},
    "00006": {"en":"POWER ASSETS",          "zh":"電能實業",          "industry":"UTL","ind_zh":"公用事業", "type":"stable"},
    "00066": {"en":"MTR CORPORATION",       "zh":"港鐵公司",          "industry":"UTL","ind_zh":"公用事業", "type":"stable"},
    "00941": {"en":"CHINA MOBILE",          "zh":"中國移動",          "industry":"TEL","ind_zh":"電訊",     "type":"stable"},
    "00762": {"en":"CHINA UNICOM",          "zh":"中國聯通",          "industry":"TEL","ind_zh":"電訊",     "type":"stable"},
    "00008": {"en":"PCCW",                  "zh":"電訊盈科",          "industry":"TEL","ind_zh":"電訊",     "type":"stable"},

    # ── 地產 ─────────────────────────────────────────────────────────────────
    "00016": {"en":"SHK PPT",               "zh":"新鴻基地產",        "industry":"REP","ind_zh":"地產",     "type":"bluechip"},
    "00012": {"en":"HENDERSON LAND",        "zh":"恒基地產",          "industry":"REP","ind_zh":"地產",     "type":"bluechip"},
    "00017": {"en":"NEW WORLD DEV",         "zh":"新世界發展",        "industry":"REP","ind_zh":"地產",     "type":"general"},
    "00083": {"en":"SINO LAND",             "zh":"信和置業",          "industry":"REP","ind_zh":"地產",     "type":"general"},
    "00101": {"en":"HANG LUNG PPT",         "zh":"恒隆地產",          "industry":"REP","ind_zh":"地產",     "type":"general"},
    "00014": {"en":"HYSAN DEV",             "zh":"希慎興業",          "industry":"REP","ind_zh":"地產",     "type":"general"},
    "01109": {"en":"CR LAND",               "zh":"華潤置地",          "industry":"REP","ind_zh":"地產",     "type":"bluechip"},
    "00960": {"en":"LONGFOR PPT",           "zh":"龍湖集團",          "industry":"REP","ind_zh":"地產",     "type":"general"},
    "00823": {"en":"LINK REIT",             "zh":"領展房產基金",      "industry":"REP","ind_zh":"地產",     "type":"stable"},

    # ── 醫療 ─────────────────────────────────────────────────────────────────
    "01177": {"en":"SINO BIOPHARM",         "zh":"中國生物製藥",      "industry":"HCR","ind_zh":"醫療",     "type":"general"},
    "01093": {"en":"CSPC PHARMA",           "zh":"石藥集團",          "industry":"HCR","ind_zh":"醫療",     "type":"general"},
    "02269": {"en":"WUXI BIO",              "zh":"藥明生物",          "industry":"HCR","ind_zh":"醫療",     "type":"bluechip"},
    "02359": {"en":"WUXI APPTEC",           "zh":"藥明康德",          "industry":"HCR","ind_zh":"醫療",     "type":"bluechip"},
    "06160": {"en":"BEIGENE-SW",            "zh":"百濟神州",          "industry":"HCR","ind_zh":"醫療",     "type":"bluechip"},
    "00013": {"en":"HUTCHMED",              "zh":"和黃醫藥",          "industry":"HCR","ind_zh":"醫療",     "type":"general"},

    # ── 工業 ─────────────────────────────────────────────────────────────────
    "06869": {"en":"YANGTZE OFC",             "zh":"長飛光纖光纜",       "industry":"IND","ind_zh":"工業",     "type":"general"},
    "00390": {"en":"CHINA RAILWAY",         "zh":"中國中鐵",          "industry":"IND","ind_zh":"工業",     "type":"stable"},
    "01186": {"en":"CR CONSTRUCTION",       "zh":"中國鐵建",          "industry":"IND","ind_zh":"工業",     "type":"stable"},
    "00187": {"en":"JINGCHENG MAC",         "zh":"景成機械",          "industry":"IND","ind_zh":"工業",     "type":"general"},
    "00038": {"en":"FIRST TRACTOR",         "zh":"中國一拖",          "industry":"IND","ind_zh":"工業",     "type":"general"},
    "06690": {"en":"HAIER SMART HOME",      "zh":"海爾智家",          "industry":"IND","ind_zh":"工業",     "type":"general"},
    "00568": {"en":"SHANDONG MOLONG",       "zh":"山東墨龍",          "industry":"IND","ind_zh":"工業",     "type":"general"},

    # ── 金屬礦產 ─────────────────────────────────────────────────────────────
    "02899": {"en":"CHALCO",                  "zh":"中國鋁業",           "industry":"MET","ind_zh":"金屬礦產", "type":"general"},
    "01088": {"en":"CHINA SHENHUA",         "zh":"中國神華",          "industry":"MET","ind_zh":"金屬礦產", "type":"stable"},
    "00358": {"en":"JIANGXI COPPER",        "zh":"江西銅業股份",      "industry":"MET","ind_zh":"金屬礦產", "type":"general"},

    # ── 運輸物流 ─────────────────────────────────────────────────────────────
    "00293": {"en":"CATHAY PAC AIR",        "zh":"國泰航空",          "industry":"TRN","ind_zh":"運輸",     "type":"bluechip"},
    "00316": {"en":"OOIL",                  "zh":"東方海外國際",      "industry":"TRN","ind_zh":"運輸",     "type":"general"},
    "01199": {"en":"COSCO SHIPPING",        "zh":"中遠海控",          "industry":"TRN","ind_zh":"運輸",     "type":"general"},
    "00144": {"en":"CM PORT",               "zh":"招商局港口",        "industry":"TRN","ind_zh":"運輸",     "type":"general"},

    # ── 零售消費 ─────────────────────────────────────────────────────────────
    "02319": {"en":"MENGNIU",               "zh":"蒙牛乳業",          "industry":"RET","ind_zh":"零售消費", "type":"general"},
    "00151": {"en":"WANT WANT CHINA",       "zh":"旺旺中國",          "industry":"RET","ind_zh":"零售消費", "type":"general"},
    "00288": {"en":"WH GROUP",              "zh":"萬洲國際",          "industry":"RET","ind_zh":"零售消費", "type":"general"},
    "00027": {"en":"GALAXY ENT",            "zh":"銀河娛樂",          "industry":"RET","ind_zh":"零售消費", "type":"bluechip"},
    "01928": {"en":"SANDS CHINA",           "zh":"金沙中國",          "industry":"RET","ind_zh":"零售消費", "type":"bluechip"},
    "09633": {"en":"NONGFU SPRING",         "zh":"農夫山泉",          "industry":"RET","ind_zh":"零售消費", "type":"bluechip"},
    "06862": {"en":"HAIDILAO",              "zh":"海底撈",            "industry":"RET","ind_zh":"零售消費", "type":"bluechip"},
    "01876": {"en":"BUDWEISER APAC",        "zh":"百威亞太",          "industry":"RET","ind_zh":"零售消費", "type":"bluechip"},
    "00168": {"en":"TSINGTAO BREW",         "zh":"青島啤酒股份",      "industry":"RET","ind_zh":"零售消費", "type":"general"},
    "00291": {"en":"CR BEER",               "zh":"華潤啤酒",          "industry":"RET","ind_zh":"零售消費", "type":"bluechip"},
    "00136": {"en":"CHINA RUYI",            "zh":"中國如意",          "industry":"RET","ind_zh":"零售消費", "type":"general"},
    "00189": {"en":"DONGYUE GROUP",         "zh":"東岳集團",          "industry":"RET","ind_zh":"零售消費", "type":"general"},

    # ── 綜合企業 ─────────────────────────────────────────────────────────────
    "00001": {"en":"CKH HOLDINGS",          "zh":"長和",              "industry":"CGM","ind_zh":"綜合企業", "type":"bluechip"},
    "00019": {"en":"SWIRE PACIFIC A",       "zh":"太古股份Ａ",        "industry":"CGM","ind_zh":"綜合企業", "type":"bluechip"},
    "00267": {"en":"CITIC",                 "zh":"中信股份",          "industry":"CGM","ind_zh":"綜合企業", "type":"bluechip"},
}

# ── Lookup helpers ────────────────────────────────────────────────────────────

def get_zh_name(code: str) -> str | None:
    entry = STOCKS.get(code.zfill(5))
    return entry["zh"] if entry else None

def get_en_name(code: str) -> str | None:
    entry = STOCKS.get(code.zfill(5))
    return entry["en"] if entry else None

def get_industry(code: str) -> tuple[str, str]:
    entry = STOCKS.get(code.zfill(5))
    return (entry["industry"], entry["ind_zh"]) if entry else ("GEN", "其他")

def get_type(code: str) -> str | None:
    entry = STOCKS.get(code.zfill(5))
    return entry["type"] if entry else None

def get_stock_info(code: str) -> dict:
    entry = STOCKS.get(code.zfill(5), {})
    return {
        "code":        code.zfill(5),
        "en":          entry.get("en", ""),
        "zh":          entry.get("zh", ""),
        "industry":    entry.get("industry", "GEN"),
        "ind_zh":      entry.get("ind_zh", "其他"),
        "type":        entry.get("type", "general"),
    }
