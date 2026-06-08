from datetime import datetime
from dateutil.relativedelta import relativedelta

_AY_TR = {
    1: "Ocak", 2: "Şubat", 3: "Mart", 4: "Nisan",
    5: "Mayıs", 6: "Haziran", 7: "Temmuz", 8: "Ağustos",
    9: "Eylül", 10: "Ekim", 11: "Kasım", 12: "Aralık",
}
_GUN_TR = {
    "Monday": "Pazartesi", "Tuesday": "Salı", "Wednesday": "Çarşamba",
    "Thursday": "Perşembe", "Friday": "Cuma", "Saturday": "Cumartesi",
    "Sunday": "Pazar",
}


def get_date_context() -> str:
    """
    Her agent system prompt'una eklenir.
    LLM doğal dil tarih ifadelerini (son 5 ay, bu ay, geçen yıl)
    bu bağlamla çözümler.
    """
    now = datetime.now()
    gun_tr = _GUN_TR.get(now.strftime("%A"), now.strftime("%A"))
    ay_tr = _AY_TR[now.month]

    # Son 6 ay (şimdiki ay dahil)
    last_6 = []
    for i in range(6):
        d = now - relativedelta(months=i)
        last_6.append((d.year, d.month, _AY_TR[d.month]))

    # Geçmiş aylar (ciro/hedef verisi olan): gelecek ayları dışarıda bırak
    gecmis_aylar = [(y, m, a) for y, m, a in last_6]

    quarter = (now.month - 1) // 3 + 1
    quarter_start_month = (quarter - 1) * 3 + 1

    def fmt(tpl):
        return f"{tpl[2]} {tpl[0]} (yıl={tpl[0]}, ay={tpl[1]})"

    return f"""## BUGÜNÜN TARİHİ — HER ZAMAN BU DEĞERLERİ KULLAN

Bugün           : {now.day} {ay_tr} {now.year}, {gun_tr}
Yıl             : {now.year}
Ay              : {now.month} ({ay_tr})
Çeyrek          : Q{quarter} ({_AY_TR[quarter_start_month]}–{ay_tr} {now.year})
YTD başlangıcı  : 1 Ocak {now.year}

DOĞAL DİL → TARİH DÖNÜŞÜMÜ:
  "bu ay"        → yıl={now.year}, ay={now.month} ({ay_tr})
  "geçen ay"     → yıl={last_6[1][0]}, ay={last_6[1][1]} ({last_6[1][2]})
  "son 3 ay"     → {', '.join(fmt(t) for t in gecmis_aylar[:3])}
  "son 5 ay"     → {', '.join(fmt(t) for t in gecmis_aylar[:5])}
  "son 6 ay"     → {', '.join(fmt(t) for t in gecmis_aylar[:6])}
  "bu yıl"       → yıl={now.year}
  "geçen yıl"    → yıl={now.year - 1}
  "YTD"          → {now.year} yılı 1 Ocak – {now.day} {ay_tr}

KURAL: "son N ay" dediğinde ASLA tahminde bulunma.
Yukarıdaki tablodan bak, doğru ay/yıl değerlerini kullan.
{_AY_TR.get(now.month + 1, '')} ve sonrası gibi gelecek ayları dahil ETME.
Verisi henüz gelmemiş cari ay ({ay_tr} {now.year}) için uyarı ver.
"""
