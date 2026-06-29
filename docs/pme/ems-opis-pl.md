# Opis zewnętrznego systemu EMS — program PME Część 2 z FM

**Magazyn energii** · mikroinstalacja OZE · falownik hybrydowy SRNE · SolarAssistant · Solar Smart (Raspberry Pi 4)

---

## 1. Przedmiot i zakres

Niniejszy dokument opisuje **funkcję zarządzania energią (EMS)** realizowaną przez zewnętrzny system inteligentnego sterowania przepływem energii elektrycznej pozyskanej z OZE pomiędzy:

- bieżącym zużyciem urządzeń w gospodarstwie domowym,
- magazynem energii elektrycznej,
- siecią elektroenergetyczną.

W instalacji **nie występuje magazyn ciepła**. Łańcuch priorytetów dla tego obiektu ma postać:

**zużycie → magazyn energii → sieć**

---

## 2. Architektura systemu EMS

| Element | Funkcja |
|---------|---------|
| Falownik hybrydowy **SRNE** | Sterowanie przepływami PV, magazynu, obciążenia i sieci |
| **Magazyn energii** | Akumulator podłączony do falownika |
| **SolarAssistant** | Lokalna bramka REST API do falownika (harmonogramy, tryby pracy) |
| **Solar Smart** (Raspberry Pi 4, rok produkcji 2021) | Zewnętrzny EMS: prognozy, plan dobowy, automatyzacja przy włączonym trybie Smart |
| Baza historii invertera (**IHDB**) | Dane pomiarowe rzeczywistego zużycia i produkcji |
| **Cloudflare Access** | Zdalny, uwierzytelniony dostęp do interfejsu EMS |

EMS nie jest wyłącznie funkcją wbudowaną w falownik — stanowi **osobny system sterowania** działający na kontrolerze Raspberry Pi i komunikujący się z falownikiem przez SolarAssistant.

---

## 3. Algorytm sterowania — priorytety i ograniczenia

### 3.1. Kolejność priorytetów (zgodna z programem PME)

1. **Bezpośrednie pokrycie zapotrzebowania** — energia z PV zasila bieżące obciążenie domu.
2. **Magazynowanie energii elektrycznej** — nadwyżka PV po pokryciu obciążenia ładuje akumulator.
3. **Oddanie nadwyżki do sieci** — wyłącznie po zaspokojeniu zużycia i naładowaniu magazynu w dostępnej pojemności, z dodatkowymi ograniczeniami opisanymi poniżej.

Na falowniku utrzymywany jest priorytet **Load first** (Solar power priority).

### 3.2. Planowanie co 15 minut

Obliczenia prowadzone są w module optymalizacji (`plan_optimizer.py`, `plan_spill.py`) z wykorzystaniem m.in. funkcji:

- `pv_load_energy_split` — podział energii PV między obciążenie a nadwyżkę;
- `simulate_hour` — symulacja jednego kroku czasowego;
- `_reserve_soc_kwh_from_step` — wyznaczenie rezerwy energii w magazynie do momentu, gdy PV ponownie pokryje obciążenie domu;
- `build_extended_pv_load_for_reserve` — rozszerzenie prognozy na kolejny dzień wyłącznie w celu poprawnego wyliczenia rezerwy nocnej;
- `battery_export_break_even_rce`, `battery_export_step_allowed` — warunki dopuszczalności eksportu z magazynu;
- `_allow_grid_charge` — dozwolone ładowanie z sieci tylko w taryfie off-peak i tylko w celu odtworzenia rezerwy lub pokrycia deficytu przy braku PV.

**Kolejność w praktyce:**

1. PV → obciążenie; przy deficycie → magazyn; dopiero reszta → import z sieci.
2. Nadwyżka PV → ładowanie magazynu; eksport PV tylko przy pełnym magazynie.
3. Rezerwa nocna — magazyn nie może być rozładowany poniżej poziomu wymaganego do zasilania domu do rana (wg prognozy obciążenia i PV).
4. Eksport z magazynu — tylko ponad rezerwę i tylko gdy cena sprzedaży (RCE) przewyższa wartość autokonsumpcji przy taryfie G12 off-peak; w przeciwnym razie energia pozostaje na potrzeby gospodarstwa.
5. Ładowanie z sieci w nocy — wyłącznie off-peak, wyłącznie dla rezerwy lub deficytu bez PV.

Plan dobowy jest odnawiany co godzinę (`build_plan_simulation`).

### 3.3. Realizacja na falowniku (tryb Smart)

Przy włączonym **Smart mode** plan jest przekładany na polecenia SolarAssistant:

| Funkcja | Działanie |
|---------|-----------|
| `build_sa_schedule_from_hour_row` | Ustawienie harmonogramu ładowania/rozładowania na następną godzinę |
| `run_work_mode_hour_start` | Tryb **On-grid** przy zaplanowanym rozładowaniu do sieci lub SOC 100% |
| `run_work_mode_limit_home` | Co 15 minut: **Limit power to home load** — ograniczenie niekontrolowanego eksportu, gdy brak aktywnego okna rozładowania w harmonogramie |

---

## 4. Ograniczenie eksportu w godzinach szczytowej produkcji PV

System realizuje wymóg ograniczenia **niekontrolowanego eksportu** w szczycie produkcji fotowoltaicznej poprzez:

- brak planowania eksportu z magazynu w godzinach dziennych przy typowych niskich cenach RCE względem autokonsumpcji (G12);
- eksport nadwyżki PV do sieci wyłącznie po zapełnieniu magazynu;
- wymuszanie trybu **Limit home** na falowniku poza zaplanowanym oknem rozładowania;
- rezerwę SOC uniemożliwiającą rozładowanie magazynu „pod eksport” kosztem zasilania domu do rana.

---

## 5. Praca wyspowa (zasilanie awaryjne)

Możliwość **pracy wyspowej** zapewniona jest na poziomie **instalacji elektrycznej**, niezależnie od oprogramowania EMS.

| Tryb pracy | Opis |
|------------|------|
| **Domyślny — przyłączenie do sieci** | Instalacja pracuje on-grid. Przy zaniku napięcia w sieci dystrybucyjnej zasilanie w domu jest wyłączane. |
| **Autonomiczny — ręczne przełączenie** | Operator instalacji przełącza wyłącznik/automat na obwód awaryjny. Gospodarstwo domowe jest zasilane z magazynu energii przez falownik **niezależnie od stanu sieci** elektroenergetycznej. |

Przełączenie w tryb autonomiczny jest świadomą czynnością operatora (hardware). Solar Smart nie steruje automatem wyspowym. Do dokumentacji instalacji dołącza się schemat obwodu awaryjnego oraz procedurę przełączenia.

---

## 6. Cyberbezpieczeństwo

Zgodnie z warunkami programu, wymogi wynikające z dokumentów UE (NIS2, CRA, RED DA) oraz normy ETSI EN 303 645 dotyczą urządzeń **wyprodukowanych po dacie wejścia w życie** tych aktów.

- **Raspberry Pi 4** (host EMS) — rok produkcji **2021**, przed obowiązkowym RED DA (01.08.2025) i pełnym CRA (11.12.2027).
- **Solar Smart** — wdrożenie własne na istniejącym sprzęcie.
- **Falownik i magazyn** — wymogi cyber dla urządzeń wyprodukowanych po 08.2025 leżą po stronie producenta sprzętu.

Stosowane środki ochrony EMS: Cloudflare Access, brak publicznego dostępu do API bez uwierzytelnienia, hasło SolarAssistant w konfiguracji lokalnej, aktualizacje systemu, usługa systemowa z autostartem (`smart.service`).

---

## 7. Podsumowanie

Zewnętrzny system EMS **Solar Smart** na Raspberry Pi współpracuje z falownikiem SRNE przez SolarAssistant i realizuje inteligentne sterowanie przepływem energii zgodnie z priorytetami programu PME dla magazynu energii, z ograniczeniem niekontrolowanego eksportu w szczycie produkcji PV oraz z możliwością pracy wyspowej zapewnioną na poziomie instalacji elektrycznej.
