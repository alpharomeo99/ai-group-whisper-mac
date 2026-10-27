"""Daily Batch Dialogue Engine.

Pre-generates organic multi-persona daily conversational arcs for groups once a day.
Dormant accounts connect on-demand at scheduled timestamps to type and send their message,
then disconnect immediately.

Live AI Orchestration triggers ONLY when an external human speaks in the group.
Bot-to-bot interactions are strictly governed by this daily batch schedule.
"""
import asyncio
import json
import logging
import math
import random
import re
import time
from datetime import datetime
import ai

log = logging.getLogger("whisper.batch")

COMMON_TYPOS = {
    "their": "thier",
    "there": "ther",
    "weight": "weihgt",
    "protein": "prtoein",
    "dosing": "dosign",
    "tomorrow": "tommorow",
    "because": "becasue",
    "definitely": "definately",
    "honestly": "honstly",
    "running": "runnign",
    "fatigue": "fatgiue",
    "sleep": "slepe",
    "morning": "mornign",
    "cardio": "cardoi",
    "about": "abotu",
}

def maybe_inject_typo_correction(text, chance=0.07):
    """Introduces realistic fast thumb typos with an immediate follow-up asterisk bubble."""
    if not text or len(text) < 18 or random.random() > chance or " ||| " in text:
        return text
    words = text.split()
    for idx, w in enumerate(words):
        clean_w = re.sub(r'[^\w]', '', w.lower())
        if clean_w in COMMON_TYPOS:
            typo_word = COMMON_TYPOS[clean_w]
            words[idx] = re.sub(clean_w, typo_word, w, flags=re.IGNORECASE)
            correction = f"*{clean_w}" if random.random() < 0.65 else f"*{clean_w} lol"
            return " ".join(words) + " ||| " + correction
    return text

def format_relationship_graph(candidates):
    """Constructs dynamic relationship pairings and shared banter context between active group personas."""
    if len(candidates) < 2:
        return "Regular members who chat casually."
    names = [c.get("persona_name") or c.get("account_name") for c in candidates if c.get("persona_name") or c.get("account_name")]
    if len(names) < 2:
        return "Regular members who chat casually."
    dynamics = [
        "Gym buddies: train heavy together, banter about leg day suffering and PRs.",
        "Friendly skeptics: one asks for lab numbers and science when the other hypes a new protocol.",
        "Supp banter duo: constantly roast each other's supplement stacks and overthinking.",
        "Veteran & learner: one shares hard-earned lessons, the other asks practical everyday tips.",
        "Work vs gym contrast: one complains about their corporate desk job, the other works freelance/trades."
    ]
    pairs = []
    shuffled = list(names)
    random.seed(int(time.time() // 86400))
    random.shuffle(shuffled)
    for i in range(0, len(shuffled) - 1, 2):
        dyn = dynamics[(i // 2) % len(dynamics)]
        pairs.append(f"{shuffled[i]} & {shuffled[i+1]} ({dyn})")
    random.seed()
    return "; ".join(pairs) if pairs else "Close community regulars who know each other's routines."



class DailyBatchEngine:
    def is_candidate_awake(self, cand, ts=None):
        """Checks if a persona is awake at a given timestamp according to their individual sleep window."""
        if ts is None:
            ts = time.time()
        from datetime import datetime as _dt
        local_dt = _dt.fromtimestamp(ts)
        local_hour = local_dt.hour + (local_dt.minute / 60.0)

        # Daytime hours (08:00 to 23:30) are 100% active and awake for all personas
        if 8.0 <= local_hour < 23.5:
            return True

        p_seed_str = str(cand.get("persona_id") or cand.get("id") or cand.get("persona_name") or "default")
        seed_hash = sum(ord(c) for c in p_seed_str)
        archetype_idx = seed_hash % 6
        SCHEDULES = [
            (23.5, 6.5),  # early_bird
            (0.5, 7.5),   # standard
            (1.5, 8.0),   # moderate_night_owl
            (2.0, 8.5),   # late_night_owl
            (2.5, 8.5),   # night_owl_bro
            (23.0, 6.0),  # morning_athlete
        ]
        s_start, s_end = SCHEDULES[archetype_idx]
        if s_start <= s_end:
            asleep = (s_start <= local_hour < s_end)
        else:
            asleep = (local_hour >= s_start or local_hour < s_end)
        return not asleep

    def __init__(self, store, ai_module, memory_engine=None):
        self.store = store
        self.ai = ai_module
        self.memory = memory_engine

    def today_str(self):
        return datetime.now().strftime("%Y-%m-%d")

    def get_group_candidates(self, chat_id):
        """Candidate resolution strictly limited to accounts and personas explicitly assigned or belonging to this group."""
        chat_id = int(chat_id)
        candidates = []
        seen = set()

        # 1. group_personas with explicit account_id
        gp_rows = self.store.rows(
            "SELECT p.id as persona_id, p.name as persona_name, p.details, p.prompt, gp.account_id, a.name as account_name, a.phone as account_phone, a.username as account_username "
            "FROM group_personas gp "
            "JOIN personas p ON p.id=gp.persona_id "
            "JOIN accounts a ON a.id=gp.account_id "
            "WHERE gp.chat_id=? AND a.active=1",
            (chat_id,)
        )
        for r in gp_rows:
            key = (r["persona_id"], r["account_id"])
            if key not in seen:
                candidates.append(r)
                seen.add(key)

        # 2. group_accounts where account has persona_id assigned
        ga_rows = self.store.rows(
            "SELECT p.id as persona_id, p.name as persona_name, p.details, p.prompt, a.id as account_id, a.name as account_name, a.phone as account_phone, a.username as account_username "
            "FROM group_accounts ga "
            "JOIN accounts a ON a.id=ga.account_id "
            "JOIN personas p ON p.id=a.persona_id "
            "WHERE ga.chat_id=? AND a.active=1",
            (chat_id,)
        )
        for r in ga_rows:
            key = (r["persona_id"], r["account_id"])
            if key not in seen:
                candidates.append(r)
                seen.add(key)

        # 3. Group owner account's persona if explicitly linked to this group
        g_rows = self.store.rows("SELECT account_id FROM groups WHERE chat_id=?", (chat_id,))
        if g_rows and g_rows[0].get("account_id"):
            g_aid = g_rows[0]["account_id"]
            owner_rows = self.store.rows(
                "SELECT p.id as persona_id, p.name as persona_name, p.details, p.prompt, a.id as account_id, a.name as account_name, a.phone as account_phone, a.username as account_username "
                "FROM accounts a "
                "JOIN personas p ON p.id=a.persona_id "
                "WHERE a.id=? AND a.active=1",
                (g_aid,)
            )
            for r in owner_rows:
                key = (r["persona_id"], r["account_id"])
                if key not in seen:
                    candidates.append(r)
                    seen.add(key)

        # 4. Broad Fallback for Watched Groups: Ensure all active accounts with personas participate
        g_watch = self.store.rows("SELECT watched FROM groups WHERE chat_id=?", (chat_id,))
        is_watched = bool(g_watch and g_watch[0].get("watched"))
        if is_watched or len(candidates) < 2:
            all_active = self.store.rows(
                "SELECT p.id as persona_id, p.name as persona_name, p.details, p.prompt, a.id as account_id, a.name as account_name, a.phone as account_phone, a.username as account_username "
                "FROM accounts a "
                "JOIN personas p ON p.id=a.persona_id "
                "WHERE a.active=1"
            )
            for r in all_active:
                key = (r["persona_id"], r["account_id"])
                if key not in seen:
                    candidates.append(r)
                    seen.add(key)

        # 5. Guaranteed Participation Fallback: Pair active accounts with available personas
        if len(candidates) < 2:
            all_personas = self.store.rows("SELECT id as persona_id, name as persona_name, details, prompt FROM personas")
            all_active_accs = self.store.rows("SELECT id as account_id, name as account_name, phone as account_phone, username as account_username FROM accounts WHERE active=1")
            for i, acc in enumerate(all_active_accs):
                if all_personas:
                    p = all_personas[i % len(all_personas)]
                    cand = {**p, **acc}
                    key = (cand["persona_id"], cand["account_id"])
                    if key not in seen:
                        candidates.append(cand)
                        seen.add(key)

        return candidates

    async def generate_all_groups(self, count=None):
        """Generates today's batch schedule for all eligible groups in one shot."""
        if count is None:
            count = int(self.store.get("batch_messages_count", 24))
        today = self.today_str()
        groups = self.store.rows("SELECT * FROM groups WHERE watched=1")
        all_cids = [g["chat_id"] for g in groups]

        results = []
        for cid in all_cids:
            cands = self.get_group_candidates(cid)
            if len(cands) >= 2:
                try:
                    res = await self.generate_batch_for_group(cid, count=count)
                    g_match = [g for g in groups if g["chat_id"] == cid]
                    title = g_match[0].get("title") if g_match else f"Group {cid}"
                    results.append({"chat_id": cid, "title": title, "count": res.get("count", 0)})
                except Exception as e:
                    log.warning("Failed to generate batch for group %s: %s", cid, e)
        return results

    async def generate_batch_for_group(self, chat_id, count=48, topic_override=None):
        """Generates today's batch schedule for a watched group."""
        chat_id = int(chat_id)
        g_rows = self.store.rows("SELECT * FROM groups WHERE chat_id=?", (chat_id,))
        if not g_rows:
            raise ValueError(f"Group {chat_id} not found")
        group = g_rows[0]

        # 1. Fetch assigned personas with accounts using unified candidate resolver
        candidates = self.get_group_candidates(chat_id)

        if not candidates:
            raise ValueError("No active accounts with personas found for this group. Link personas to accounts first.")

        # 2. Get recent chat context for continuity
        recent_msgs = self.store.recent(chat_id, limit=12)
        transcript_lines = []
        for m in recent_msgs:
            if m.get("text"):
                transcript_lines.append(f"[{m.get('sender', 'Member')}]: {m.get('text')}")
        recent_transcript = "\n".join(transcript_lines) or "(Group chat is fresh / quiet)"

        # 2B. Build ABSOLUTE ZERO REPETITION blacklist from the last 48 hours
        now_ts = int(time.time())
        past_batch_msgs = self.store.rows(
            "SELECT text FROM daily_batch_items WHERE chat_id=? AND (created_at > ? OR scheduled_ts > ?) "
            "ORDER BY id DESC LIMIT 50",
            (chat_id, now_ts - 172800, now_ts - 172800)
        )
        forbidden_snippets = []
        for pbm in past_batch_msgs:
            t = (pbm.get("text") or "").strip()
            if t and len(t) > 12 and not t.startswith("[reaction:"):
                forbidden_snippets.append(t[:75].replace("\n", " "))
        for rm in recent_msgs:
            t = (rm.get("text") or "").strip()
            if t and len(t) > 12:
                forbidden_snippets.append(t[:75].replace("\n", " "))

        forbidden_snippets = list(dict.fromkeys(forbidden_snippets))[:30]
        forbidden_block = "\n".join(f"  * {s}" for s in forbidden_snippets) if forbidden_snippets else "  * (None yet - keep discussion completely fresh)"

        # 2C. Determine topic context with strict rotation across unused themes
        recent_batches = self.store.rows(
            "SELECT topic FROM daily_batches WHERE chat_id=? ORDER BY id DESC LIMIT 15",
            (chat_id,)
        )
        used_past_topics = [b.get("topic") or "" for b in recent_batches]

        default_themes = [
            ("Tendon & joint healing protocols: BPC-157 vs TB-500 experiences, golfer's elbow, rotator cuff flare-ups, recovery timelines.", "bpc tb500 healing tendon joint elbow"),
            ("GLP-1 appetite suppression & cutting: Tirzepatide vs Retatrutide vs Semaglutide, 'food noise' going silent, nausea management, dosing titration.", "tirz reta sema glp1 food noise cut appetite"),
            ("Growth hormone secretagogues: CJC-1295 / Ipamorelin vs MK-677, vivid REM dreams, morning water retention, tingling hands and skin glow.", "cjc ipam mk677 gh dreams sleep growth"),
            ("Skin, hair & anti-aging peptides: GHK-Cu copper peptide injections vs topicals, sting management, Epithalon cellular longevity protocols.", "ghk copper epithalon skin hair antiaging"),
            ("Reconstitution chemistry & gear prep: BAC water ratios, insulin syringe gauge (29g vs 31g), fridge storage, preventing peptide degradation.", "recon bac water insulin syringe storage fridge math"),
            ("Peptides while traveling: flying through airport security with pre-mixed pens, cooling travel cases, TSA experiences.", "travel airport tsa cooling pen flight carryon"),
            ("Nootropic peptides & cognitive drive: Semax, Selank, Bromantane, conquering afternoon brain fog and workplace focus.", "semax selank bromantane nootropic focus cognitive"),
            ("Mitochondrial & endurance peptides: MOTS-c, SS-31, cellular energy output, cardio stamina boosts.", "mots ss31 mitochondria energy cardio endurance"),
            ("Third-party lab testing & HPLC verification: reading purity percentages, endotoxin testing, why cheap underground vendors cut corners.", "hplc coa purity test lab vendor testing"),
            ("Appetite ramp-ups for hardgainers: MK-677 hunger spikes, waking up at 3am raiding the fridge, managing insulin sensitivity.", "mk677 hunger appetite bulk fridge glucose"),
            ("Post-injury rehab milestones: combining physiotherapy, eccentrics, and tissue-repair peptides to get back under heavy barbells.", "rehab injury physio squat bench recovery"),
            ("Subq vs shallow IM injection debate: absorption rates, lumps/bruising, needle anxiety vs smooth pin routines.", "subq im injection pin needle bruise site"),
            ("TRT clinic optimization: navigating clinics vs self-prescribing, dialing in 100-150mg/wk, twice weekly vs microdosing every day.", "trt clinic testosterone dose frequency protocol"),
            ("High hematocrit & RBC management: donating whole blood vs therapeutic phlebotomy, staying hydrated, cardio for blood viscosity.", "hematocrit rbc blood donation hydration viscosity trt"),
            ("Estrogen management & sides: puffy nipples, mood swings, water retention, why crashing E2 with too much Arimidex feels like death.", "estrogen e2 arimidex aromasin sides nipple ai"),
            ("Pinning soreness (PIP) horror stories: virgin muscle pip, quad pinning regrets vs smooth ventroglute, warming vials before drawing.", "pip injection pain ventroglute quad delt soreness"),
            ("Bloodwork deep-dive: analyzing liver enzymes (AST/ALT), kidney eGFR, lipid panels (HDL/LDL), and when to worry vs stay chill.", "bloodwork labs ast alt hdl ldl lipids kidneys"),
            ("Hair loss & DHT sensitivity: finasteride, dutasteride, topical minoxidil, RU58841, deciding whether the hair or the delts matter more.", "hair shedding dht finasteride minoxidil baldness delts"),
            ("Cardiovascular protection on cycle: daily fish oil, citrus bergamot, CoQ10, telmisartan, monitoring resting heart rate and blood pressure.", "cardio heart blood pressure citrus bergamot bp fish oil"),
            ("Acne & skin flare-ups: backne breakout solutions, salicylic acid washes, benzoyl peroxide, when people resort to low-dose accutane.", "acne backne skin breakout accutane wash clear"),
            ("Blast & Cruise vs cycling off (PCT): why modern lifters abandon PCT, the mental crash of coming off, committing to testosterone for life.", "blast cruise pct nolva clomid crash cruise off"),
            ("Oral compounds debate: Anavar vs Turinabol vs Dianabol, pump back pumps, liver toxicity, why orals are overrated vs injectables.", "anavar tbol dbol orals pumps liver toxic"),
            ("Masteron vs Primobolan: cosmetic dry look, anti-estrogenic feeling, DHT fullness, dealing with counterfeit primo.", "primo masteron mast dry cosmetic dht fake"),
            ("First cycle regrets & mistakes: running too much too early, not having bloods done first, listening to bro-science at local gyms.", "first cycle mistake regret bloodwork broscience beginner"),
            ("Golden era bodybuilding aesthetics vs modern mass: Arnold, Frank Zane, Serge Nubret vs modern 280lb mass monsters with gut distension.", "golden era arnold zane aesthetic vacuum gut mass"),
            ("Sleep apnea & heavy lifter breathing: thick necks, snoring, CPAP machines changing life quality and daytime fatigue.", "sleep apnea cpap snore breathing neck fatigue"),
            ("RAD-140 (Testolone) experiences: aggressive gym drive, dry vascularity, joint stiffness, suppression timeline.", "rad140 testolone strength dry aggression joints"),
            ("LGD-4033 (Ligandrol) fullness vs water bloat: crazy glycogen storage, strength increases, post-cycle fatigue.", "lgd4033 ligandrol pumps bulk water bloat"),
            ("Ostarine (MK-2866) realistic expectations: mild recomp, saving muscle in a steep calorie deficit, joint soothing.", "ostarine mk2866 cut recomp joints mild"),
            ("Cardarine (GW-501516) unlimited cardio engine: dropping 5k run times, stamina, the infamous rodent cancer study debate.", "cardarine gw501516 cardio stamina endurance 5k"),
            ("Enclomiphene as a test base: keeping LH and FSH alive during research chemical runs, eye floaters risk, source purity.", "enclomiphene enclo test base lh fsh floaters"),
            ("SARM liquid vs capsules: horrible ethanol / PEG taste, dosing accuracy with oral syringes, sublingual myths.", "sarms liquid peg taste oral syringe dosing dropper"),
            ("SARM goblin culture & TikTok hype: teenagers taking compounds without knowing what testosterone is, gym memes.", "sarm goblin tiktok teen gym memes hype"),
            ("Suppression bloodwork: waking up with zero morning wood, feeling lethargic week 5, getting real blood panels.", "suppression libido bloodwork lethargy test drop"),
            ("Hollywood superhero physiques: Hugh Jackman Wolverine dry-cut, Chris Hemsworth Thor bulk, dehydration before filming scenes.", "hollywood jackman wolverine hemsworth thor superhero actor"),
            ("The Rock natty debate: Joe Rogan calling on Dwayne Johnson to admit TRT, 'just cod and 4am workouts' joke, public perception.", "the rock dwayne johnson natty trt rogan cod"),
            ("Alan Ritchson (Reacher) being refreshingly honest: talking openly about TRT saving his fatigue, shoulder reconstruction, brutal filming schedules.", "reacher alan ritchson trt honest actor series hollywood"),
            ("Kumail Nanjiani Eternals transformation: vascular jawline, internet memes, dramatic shift in body composition in his 40s.", "kumail nanjiani marvel eternals transformation memes jawline"),
            ("Zac Efron Baywatch physical toll: how awful he felt running zero carbs and water pills, mental burnout, lasting respect for the look.", "zac efron baywatch cut water pills depleted mental"),
            ("Joe Rogan podcast clips & health obsession: Joe talking about peptides (BPC-157), cold plunge, TRT, carnivore month, stem cells.", "rogan podcast jre peptides cold plunge bpc trt elk"),
            ("Derek from More Plates More Dates (MPMD): classic breakdowns, 'supraphysiological doses', gorilla mind, diagnosing Hollywood lies.", "derek mpmd more plates supraphysiological gorilla breakdown"),
            ("Andrew Huberman morning routine memes: 15 minutes of direct sunlight, cold shower, waiting 90 minutes for coffee, optimizing everything.", "huberman sunlight coffee morning routine dopamine cold shower"),
            ("Sam Sulek phenomenon: gallon of chocolate milk, Krispy Kreme pre-workout, heavy lifting style, relaxed cardio bike chats.", "sam sulek chocolate milk krispy kreme bulk cardio youtube"),
            ("Liver King downfall & aftermath: 'raw liver ancestral tenets' exposed for 1k/month growth hormone, internet roast fest.", "liver king raw liver ancestral growth hormone exposed"),
            ("Bradley Martyn 'Could you beat me in a street fight?': asking UFC champs and boxers, Zoo Culture gym antics, internet comedy.", "bradley martyn street fight zoo culture ufc meme"),
            ("Mike O'Hearn 'duck eggs & hard work' lifetime natty memes: baby don't hurt me soundtrack, defying aging, hilarious community lore.", "mike ohearn duck eggs natty baby dont hurt me meme aging"),
            ("Mark Wahlberg insane 3:30 AM daily routine: cryo chamber, prayer, 4am workout, family time, whether anyone actually lives like that.", "mark wahlberg routine 4am cryo schedule early"),
            ("Celebrity Ozempic / Tirzepatide epidemic: everyone in Hollywood suddenly dropping 30 lbs, sunken faces, public denial vs reality.", "ozempic hollywood celebrity weight loss tirzepatide face red carpet"),
            ("Bodybuilding Olympia predictions & drama: Hadi Choopan, Derek Lunsford, Samson Dauda, Chris Bumstead (CBum) Classic Physique dominance.", "olympia cbum classic physique lunsford samson bodybuilder"),
            ("Strongman & Powerlifting feats: Eddie Hall vs Thor Bjornsson 500kg deadlift history, Hafthor, Mitchell Hooper modern dominance.", "strongman eddie hall thor deadlift hooper powerlifting world record"),
            ("UFC Fight Night recap & brutal knockouts: Alex Pereira's left hook of death, Jon Jones status, crazy spinning wheel kicks.", "ufc pereira knockout fight night jon jones mma"),
            ("Boxing spectacles & influencer cards: Jake Paul fighting veterans, Mike Tyson return, circus show vs genuine entertainment.", "boxing jake paul tyson knockout fight card ppv"),
            ("Premier League & Champions League football madness: title race drama, red card controversies, late stoppage time winners.", "football soccer premier league champions league goal red card"),
            ("NFL season & physical collisions: quarterback injuries, fantasy football collapses, defensive monster tackles.", "nfl football quarterback fantasy game touchdown hits"),
            ("Formula 1 race weekend chaos: tire pit-stop disasters, qualifying speed, Max Verstappen, team radio arguments.", "f1 formula 1 racing pitstop verstappen grand prix"),
            ("Crypto market volatility & wild swings: Bitcoin hitting new records or crashing overnight, memecoins, hilarious wallet losses.", "crypto bitcoin btc dip bull bear wallet memecoin"),
            ("Stock market & inflation complaints: grocery store shocks,  for a dozen eggs, ribeye steak prices, cost of living.", "stocks inflation eggs groceries meat steak cost economy"),
            ("Crazy viral internet news: bizarre Florida man headlines, wild viral TikTok trends, unbelievable real-world stories.", "viral news florida man crazy headlines internet story"),
            ("Artificial Intelligence & humanoid robots: Boston Dynamics backflips, ChatGPT replacing office tasks, robot anxiety.", "ai tech robot openai chatgpt future humanoid automation"),
            ("Blockbuster movies & binge TV shows: new cinema releases, sci-fi series, HBO dramas, what people are watching during cardio.", "movies show hbo cinema netflix watch cardio series"),
            ("Severe weather extremes: heatwaves melting asphalt, blizzard snowed-in gym cancellations, working out in unheated garages.", "weather storm heatwave blizzard winter garage gym freeze"),
            ("Music releases & gym playlist culture: new album drops, death metal vs phonk vs 90s hip hop for hitting heavy PRs.", "music album playlist phonk metal hip hop beats gym"),
            ("Viral TikTok gym cringe & public filming: influencers setting up tripods in walkways, getting mad when someone walks by.", "tiktok cringe tripod filming walkway gym public etiquette"),
            ("Gaming & late night PC sessions: new game releases, staying up till 3 AM gaming while on a cut, discord banter.", "gaming pc steam console late night game release setup"),
            ("Corporate absurdity & corporate speak: 'let's circle back', meetings that could have been an email, replying to Slack on the bench press.", "corporate job boss slack email meeting office work"),
            ("Remote work perks & struggles: lifting at 2 PM on a Tuesday, mouse-jiggler paranoia, home office cabin fever.", "remote work wfh home office zoom camera gym midday"),
            ("Trades & blue collar vs office workers banter: trades guys laughing at desk job hand calluses, working in the freezing cold.", "trades blue collar construction desk job hands work hard"),
            ("Side hustles & entrepreneurial ideas: e-commerce, consulting, flipping gear, trying to escape the 9-to-5 grind.", "hustle side business ecommerce money grind quit job"),
            ("Monday morning survival mode: double scoop pre-workout, 5 cups of black coffee, barely surviving the 9 AM team standup.", "monday morning coffee preworkout tired work dread survive"),
            ("Job interviews & salary negotiations: bluffing on resumes, funny interview questions, securing the bag.", "interview job salary offer resume negotiation hiring"),
            ("Burnout & work-life balance: working 60-hour weeks while trying to hit the gym 5 days, recovery collapse.", "burnout stress hours work overtime balance fatigue life"),
            ("Work travel survival: hotel gyms with one 35lb dumbbell, eating airport salads, keeping up routines on the road.", "hotel gym business trip travel airport workout dumbbell"),
            ("Coworkers noticing physique changes: 'what are you eating?', questions about protein shakes, desk snacks drama.", "coworker office food comments shake protein snacks questions"),
            ("Getting laid off or switching careers: taking a leap of faith, severance pay, using free time to get in the best shape ever.", "career switch layoff job hunt change severance time grind"),
            ("Gym life vs romantic partner friction: partner complaining about 6 tupperware meals in the fridge and 2-hour gym sessions.", "girlfriend wife partner relationship tupperware mealprep gym time"),
            ("Modern dating app horror stories: Hinge / Tinder disaster dates, catfishing, bizarre first date conversations.", "dating tinder hinge date dinner awkward story ghosted"),
            ("Gym crushes & awkward moments: accidental eye contact across the cable cross, dropping a weight, trying to play it cool.", "gym crush eye contact awkward cable dumbbell pretend smooth"),
            ("Social events while on a strict cut: going to weddings or dinners, sipping sparkling water with lime, dodging dessert pressure.", "wedding dinner social cut alcohol sparkling water diet peer pressure"),
            ("Trying to fit into formal clothes after a bulk: suit jackets ripping at the shoulders, thighs choking in dress pants.", "suit pants clothes bulk dress formal wedding tight shoulders"),
            ("Dating another lifter vs non-lifter: sharing macros, spotting each other, or someone who doesn't understand why you weigh rice.", "dating lifter weigh food macros gym couple rice scale"),
            ("Nightlife & alcohol trade-offs: the brutal 3-day hangover after turning 28, how alcohol ruins protein synthesis and sleep scores.", "alcohol hangover drink beer party sleep recovery headache"),
            ("Family gatherings & unsolicited advice: aunts and uncles saying 'you're getting too big' or 'careful with those powders'.", "family holiday dinner comments too big powder parents aunt"),
            ("Planning vacations around gym access: checking Google Maps for gyms near the hotel before booking the flight.", "vacation holiday hotel gym maps beach travel workout"),
            ("Balancing kids, family duties and training: waking up at 5 AM before the kids wake up, garage gym parenting.", "kids family parenting early 5am morning garage schedule"),
            ("Costco & grocery bulk hauls: clearing out 30 lbs of chicken breast, 10 cartons of egg whites, huge Greek yogurt tubs.", "costco grocery haul chicken egg whites yogurt bulk cart"),
            ("Protein ice cream & Ninja Creami obsessions: xanthan gum disasters, pint turning into a solid block of ice, perfection recipes.", "ninja creami protein ice cream xanthan frozen recipe snack"),
            ("The legendary post-cut cheat meal: whole pizza, burgers, box of donuts, waking up with swollen ankles and 10 lbs of water.", "cheat meal pizza burger donuts feast water weight bloat"),
            ("Digestive bloat & gut health: digestive enzymes, feeling like a hot air balloon after 400g of carbs, pepcid and ginger.", "digestion bloat enzymes carbs stomach gas food gut"),
            ("Air fryer supremacy: cooking chicken, crispy potatoes, steak bites in 12 minutes, the greatest invention for meal prep.", "airfryer chicken potatoes crispy mealprep fast easy cooking"),
            ("Chicken breast fatigue & marinades: tired of plain poultry, discovering Greek yogurt marinades, hot sauces, switching to lean beef.", "chicken breast dry hot sauce beef marinade cook flavor tired"),
            ("Ground beef vs rice ratio: the classic monster mash bowl, bone broth, easy digestion when force-feeding calories.", "ground beef rice monster mash bowl digest calories meal"),
            ("Protein bars tasting like sweetened chalk: ranking bars, microwave tricks, stomach cramps from sugar alcohols.", "protein bar chalk taste microwave snack sugar alcohol"),
            ("Late night hunger hacks: sugar-free jello, massive bowls of popcorn, sparkling water to trick the brain while cutting.", "late night hunger jello popcorn diet volume low cal hack"),
            ("Cooking fails & burned meal prep: setting off smoke alarms at midnight cooking 5 lbs of chicken, smoking the kitchen out.", "cooking fail burned mealprep smoke alarm kitchen disaster ruined"),
            ("Weird characters at the local commercial gym: the guy lifting in work boots and jeans, the shadowboxer by the mirrors.", "weird gym characters boots jeans shadowboxer mirror funny"),
            ("Sauna regulars & crazy conversations: old dudes having loud geopolitical debates completely naked, pouring cold water on the sensor.", "sauna steam room old guys debate hot heat sensor etiquette"),
            ("The gym music battlefield: gym speakers playing soft acoustic pop while someone is attempting an 800lb deadlift PR.", "gym music speakers radio pop terrible songs loud headphones"),
            ("Random 2 AM shower thoughts: could a silverback gorilla bench press 1,500 lbs, how many 5th graders could you fight off.", "shower thoughts gorilla bench 2am funny debate silly thoughts"),
            ("Zombie apocalypse gym squad: picking 3 members of the group chat for the survival team based on physical attributes.", "zombie apocalypse squad team survival bunker gym group chat"),
            ("Unserious food debates: is a hot dog technically a sandwich, pineapple on pizza, ranking fast food french fries.", "hot dog sandwich debate pineapple pizza fries fast food stupid"),
            ("Equipment hogs & gym etiquette: someone putting a towel and water bottle on 4 machines simultaneously at peak 5 PM hours.", "equipment hog towel water bottle machines etiquette peak hours rude"),
            ("The dreaded gym bathroom horror stories: walking in after leg day, people taking pre-workout dumps, no paper towels.", "bathroom locker room preworkout leg day horror funny gym"),
            ("Apple Watch & fitness tracker obsession: closing rings, checking sleep tracking scores, obsessing over resting heart rate.", "apple watch whoop oura sleep score rings tracker steps heart"),
            ("Old gym injuries & dumb childhood stunts: breaking an arm jumping off a shed at 10 years old, dropping a 25lb plate on a toe.", "injury childhood dumb story broke bone toe dropped plate hospital"),
            ("Shoes for lifting debate: barefoot / socks vs Converse Chuck Taylors vs Vans vs expensive squat lifting shoes.", "shoes lifting converse vans barefoot squat shoes flat sole"),
            ("Pre-workout side effects: face itching from beta-alanine, heart racing, feeling like you can fight a bear in traffic.", "preworkout beta alanine tingles itch heart pump energy scoops"),
            ("Lifting belt & gear addiction: lever belt vs prong, knee sleeves smelling like biological hazard, wrist wraps.", "lever belt sbd inzer knee sleeves smell wrist wraps gear gym"),
            ("Existential gym thoughts: why do we lift heavy iron in circles just to put it back down, but we wouldn't trade it for the world.", "existential lifting why iron heavy circles life love philosophy"),
        ]

        fresh_themes = [
            (t, q) for (t, q) in default_themes
            if not any(q in ut.lower() or t[:25].lower() in ut.lower() for ut in used_past_topics)
        ]
        if not fresh_themes:
            fresh_themes = default_themes

        chosen_theme, chosen_query = random.choice(fresh_themes)

        if topic_override:
            topic_prompt = topic_override
            topic_query = topic_override
        elif transcript_lines and random.random() < 0.25:
            # 25% of the time: natural transition with immediate topic pivot
            recent_snippet = " ".join(m.get("text", "") for m in recent_msgs[-3:] if m.get("text"))[:140]
            topic_prompt = (
                f"Natural conversational transition: starting from a quick casual nod to recent chatter ('{recent_snippet}'), "
                f"then immediately pivoting to a fresh angle on: {chosen_theme}. NEVER repeat previous points."
            )
            topic_query = chosen_query
        else:
            # 75% of the time: fresh topic initiation from unused pool
            topic_prompt = f"Fresh topic initiation: {chosen_theme}. One persona starts with a casual observation, question, or hot take."
            topic_query = chosen_query

        # 3. Format persona summaries with Telegram tags and distinct styles
        persona_summaries = []
        for c in candidates:
            dt = {}
            try:
                dt = json.loads(c.get("details") or "{}") if isinstance(c.get("details"), str) else (c.get("details") or {})
            except Exception:
                dt = {}
            role = dt.get("role_in_group") or dt.get("tone_and_vibe") or "Experienced regular"
            slang = dt.get("slang_and_vocabulary") or "Authentic telegram slang"
            emojis = dt.get("signature_emojis") or "💀, 😂"
            emoji_habit = dt.get("emoji_habit") or "moderate"
            typing = dt.get("typing_style") or "casual phone texting"
            uname = c.get("account_username")
            # Telegram tags removed per zero-tag mandate

            mem_lines = []
            if self.memory:
                mem_ctx = self.memory.get_prompt_memory_context(c["persona_id"], chat_id=chat_id, limit=8, query_text=topic_query)
                if mem_ctx:
                    mem_lines.append(mem_ctx)
            elif hasattr(self.store, "rows"):
                m_rows = self.store.rows(
                    "SELECT content FROM persona_memories WHERE persona_id=? AND (expires_at IS NULL OR expires_at > ?) "
                    "ORDER BY CASE retention_tier WHEN 'long' THEN 1 ELSE 2 END, salience DESC LIMIT 4",
                    (c["persona_id"], int(time.time()))
                )
                if m_rows:
                    mem_lines.append("Core Established Facts:\n" + "\n".join(f"  * {mr['content']}" for mr in m_rows))
            mem_str = ("\n  " + "\n  ".join(mem_lines)) if mem_lines else ""

            c_dt = {}
            if c.get("details"):
                try:
                    c_dt = json.loads(c["details"]) if isinstance(c["details"], str) else c["details"]
                except Exception:
                    c_dt = {}

            temp_res = ai.resolve_persona_temperament(
                name=c.get("persona_name"),
                details=c_dt,
                prompt=c.get("prompt")
            )
            c_tier = temp_res["tier"]
            if c_tier == "aggressive_low_patience":
                c_temperament = "RAW & LOW-PATIENCE INTERNET VERNACULAR (Zero filter, roasts dumb takes/questions, cuts through nonsense with sharp dismissive internet slang, e.g. 'bro what are you even saying', 'lol nah', 'who told you that garbage💀')"
            elif c_tier == "dry_sarcastic":
                c_temperament = "DRY & SKEPTICAL OBSERVER (Deadpan, rolls eyes at hype, weary one-liners, sarcastic reality checks)"
            else:
                c_temperament = "CHILL & GROUNDED PRACTICAL PEER (Calm, knowledgeable, explains reconstitution or protocol details without corporate tone)"

            persona_summaries.append(
                f"- ID: {c['persona_id']}, AccountID: {c['account_id']}, Name: {c['persona_name']}\n"
                f"  Vibe/Role: {role}\n"
                f"  Temperament & Speech Profile: [{c_tier.upper()}] - {c_temperament}\n"
                f"  Signature Emojis: {emojis} (habit: {emoji_habit})\n"
                f"  Typing: {typing}\n"
                f"  Brief: {c.get('prompt', '')[:160]}...{mem_str}"
            )
            proto_card = self.store.get_persona_protocol_card(c.get('persona_id'))
            if proto_card:
                persona_summaries[-1] += chr(10) + '  ' + str(proto_card)
        persona_str = "\n\n".join(persona_summaries)
        rel_graph = format_relationship_graph(candidates)
        persona_str += f"\n\nINTER-PERSONA RELATIONSHIP DYNAMICS:\n{rel_graph}\n(Personas banter, reference shared history, and chat like close gym/online peers without using @tags!)" 

        group_title = group.get("title") or f"Group {chat_id}"
        group_about = group.get("about") or "Telegram community discussion"
        group_domain = group.get("domain_knowledge") or "Lifting, gear, TRT, SARMs, peptides, nootropics, health, celebrity transformations & pop culture, sports & fight nights, breaking news, crypto/tech, work/dating, plus truly random everyday banter."

        system_prompt = (
            "You orchestrate realistic, organic human conversations in specialist Telegram communities.\n"
            "You write authentic, casual group chat exchanges between real people texting from their phones.\n\n"
            "CRITICAL GUIDELINES FOR AUTHENTIC HUMAN MESSAGING:\n"
            "AUTHENTIC CONVERSATIONAL FRICTION & ASYMMETRIC TEMPERAMENTS (MANDATORY):\n"
            "- Group chats are NEVER unanimous or polite corporate meetings! Members have completely different social temperaments:\n"
            "  * LOW-PATIENCE / RAW VERNACULAR PERSONAS: When they speak, they have zero corporate filter and low patience. When someone asks a silly question, overcomplicates basic protocols, or drops bad takes, they fire back with raw, aggressive, low-patience internet vernacular:\n"
            "    'bro what are you even saying rn', 'who told you that garbage💀', 'lol nah', 'are you serious with this question', 'stop overcomplicating basic shit and just train', 'why would you even touch that'.\n"
            "    They do NOT sugarcoat or write polite explanations.\n"
            "  * DRY / SARCASTIC PERSONAS: Drop deadpan, cynical one-liners, rolling their eyes at gym drama or over-hyped compounds ('sure bro whatever you say', 'rip your sleep schedule', 'fascinating theory💀').\n"
            "  * CHILL / GROUNDED PERSONAS: Balance the room out. They are calm, experienced, and give straightforward practical answers and reconstitution math without getting heated.\n"
            "- The low-patience members roast and push back, the skeptics tease, and the chill members balance it out. This asymmetrical friction is what makes the group feel authentic and alive!\n\n"
            "CRITICAL: ABSOLUTE ZERO REPETITION MANDATE (STRICTLY ENFORCED):\n" 
            "Under NO circumstances should any persona repeat, rehash, copy, or paraphrase any of these recently discussed points, questions, jokes, or statements:\n"
            f"{forbidden_block}\n"
            "- Do NOT re-ask questions that were already discussed.\n"
            "- Do NOT re-tell stories, cycles, or routines that were already mentioned.\n"
            "- Introduce 100% fresh angles, new exercises, novel questions, or everyday banter.\n\n"
            "TELEGRAM NATIVE EMOJI REACTIONS (MANDATORY 15-25% OF TURNS):\n"
            "- Real people frequently react with emojis (🔥, 😂, 💀, 👍, 👀, 💯, 💪, ❤️) instead of typing full text!\n"
            "- For 2 to 5 turns in this dialogue, return a native reaction by setting:\n"
            "  \"reaction\": \"🔥\", \"reply_to_step\": <target step number>, \"text\": \"\"\n"
            "- This makes the group feel authentic and buzzing without cluttering the chat with repetitive text.\n\n"

            "1. WILDLY ASYMMETRICAL LENGTH DIVERSITY (MANDATORY):\n"
            "   - Real humans NEVER type uniform-length messages! Distribute message lengths naturally:\n"
            "     * ULTRA-SHORT REACTIONS (25-30% of turns): 1 to 4 words. Examples: 'lol nah', 'fr tho💀', 'wait what?', 'bro😭', 'nah 2ml is plenty', 'same honestly', 'legend', 'yea 100%'.\n"
            "     * QUICK TAKES & QUESTIONS (40-45% of turns): 5 to 14 words. Examples: 'how was the fatigue on heavy squat day?', 'dropped caffeine after 1pm and sleep is way better', 'did you run subq or im for that?'.\n"
            "     * DETAILED REPLIES & EXPLANATIONS (25-30% of turns): 18 to 45 words. Examples: 'if it\'s a 5mg vial and you put in 2ml bac water, every 10 units on an insulin pin is 250mcg. super straightforward once you do it once, just draw slow so no air bubbles'.\n"
            "     * MULTI-MESSAGE BURSTS: Mobile texters often send 2 quick consecutive bubbles instead of one chunk. If fitting for the persona, separate consecutive bubbles with ' ||| ' (e.g. 'wait did you check the vial? ||| 5mg vs 10mg is totally different math').\n"
            "   - NEVER make every message the same length!\n\n"
            "2. GENUINE TELEGRAM REPLIES & THREADING:\n"
            "   - Group dialogue happens in conversational clusters of 2-3 messages.\n"
            "   - When a persona is responding to, answering, or bantering with a previous message in the conversation, specify 'reply_to_step': <0-indexed step of target message>.\n"
            "   - If starting a new conversational topic or check-in, set 'reply_to_step': null.\n\n"
            "3. ZERO @TAGS OR NAMES (STRICT):\n"
            "   - Never type @handles, usernames or call people by name. Address someone ONLY by setting reply_to_step to their message.\n"
            "   - Admin/owner handles orders: say 'dm admin' / 'ask the owner', never a handle.\n\n"
            "4. NATURAL EMOJIS & EXPRESSION:\n"
            "   - Use emojis naturally (💀, 😂, 😭, 🔥, 👀, 💪, 🤷‍♂️, 🤝, 🫠) according to each persona's signature emojis. Don't spam, but use them for reactions, jokes, and emphasis.\n\n"
            "5. REAL MOBILE TEXTING IMPERFECTIONS & CASUAL TYPING:\n"
            "   - Real people type fast on phone glass screens with their thumbs. They make mistakes!\n"
            "   - Skip apostrophes on common contractions ('dont', 'cant', 'wont', 'im', 'youre', 'thats', 'its').\n"
            "   - Use lowercase starts, missing periods at the end of short messages, and casual abbreviations ('rn', 'tbh', 'idk', 'ngl', 'prob', 'def', 'bcuz', 'kinda').\n"
            "   - Occasional natural phone typos when typing fast ('teh', 'yea', 'definately'). No one texts like an academic textbook!\n\n"
            "5B. REAL HUMAN BEHAVIOR (MANDATORY):\n"
            "   - Each persona has a mood today (tired, hyped, annoyed, bored, in a hurry, chill) that colors how they text. Keep it consistent in this batch.\n"
            "   - People have lives: they mention being at work, on the toilet, driving, at their kid's thing, just woke up, about to train. They sometimes leave mid-convo ('brb boss walking by').\n"
            "   - Not every message gets an answer. Some questions are ignored, some get answered late, people misread and get corrected, people say 'wait what' or 'nvm'.\n"
            "   - People disagree, tease, get mildly salty, change their mind, admit they were wrong, and go off-topic. Avoid everyone being agreeable and helpful.\n"
            "   - Callbacks and inside jokes: reference things said earlier or in memories ('like your 6am cardio era lol').\n"
            "   - Vary energy: some members barely talk and drop one-liners, some ramble. Uneven, messy, real. Never sound like customer support or an AI.\n"
            "   - Sometimes a message is just an emoji, 'lmaooo', a voice-of-text reaction, or a correction like '*their'.\n"
            "   - Opinions come from personal experience ('when i tried it...') not textbook facts. Uncertainty is normal ('idk tbh', 'could be wrong').\n\n"
            "6. DISTINCT PERSONALITIES & MANDATORY EQUAL ROTATION (NO MONOPOLIES):\n"
            "   - EVERY SINGLE persona listed under AVAILABLE PERSONAS MUST speak in this dialogue thread!\n"
            "   - Distribute speaking turns evenly across ALL candidates. Do NOT let one persona dominate the chat.\n"
            "   - Quieter or supporting personas must ask questions, banter, drop short reactions (1-4 words), or follow up.\n"
            "   - Ban single-persona monopolization: no single persona may take more than 35% of the total dialogue steps.\n"
            "   - Give each participant their own unique tone, opinions, and attitude. Do not make everyone agree or sound like clones.\n"
            "   - Stop constantly agreeing or validating. Ban 'Makes sense', 'For sure', 'Definitely', 'Agreed', 'Spot on', 'Good point', '100% facts'. Real people banter, tease, share hot takes, ask questions, or react directly.\n\n"
            "7. TOPIC SPECTRUM (FITNESS, NOOTROPICS, WELLNESS, HELPING OUT & BANTER):\n"
            "   - General fitness: PPL, lifting splits, PRs, squat/bench/deadlift form, volume management, deloads, cutting/bulking, nutrition.\n"
            "   - Nootropics & focus: Semax, Selank, Bromantane, L-Tyrosine, Alpha-GPC, clean drive, avoiding caffeine crashes.\n"
            "   - Wellness & longevity: Sleep quality, magnesium, sauna, cold plunges, joint recovery, hydration.\n"
            "   - Helping out: Welcoming peers who ask questions, giving clear, casual reconstitution math or tips.\n"
            "   - Unserious banter & jokes: Cracking jokes, playful teasing, gym humor, meme moments, and laughing off tough days.\n\n"
            "8. VENDOR POLICY:\n"
            "   - Do NOT initiate unprompted conversations about vendors, suppliers, or purchasing. All personas have had smooth, positive peer experiences.\n\n"
            "9. OUTPUT FORMAT: Return ONLY a valid JSON array of message objects matching this exact format:\n"
            "[\n"
            "  {\n"
            "    \"persona_id\": <int>,\n"
            "    \"account_id\": <int>,\n"
            "    \"sender_name\": \"<string>\",\n"
            "    \"reply_to_step\": <int or null>,\n"
            "    \"offset_minutes\": <int>,\n"
            "    \"text\": \"<exact telegram message text>\",\n"
            "    \"reaction\": \"<optional single emoji like 🔥, 😂, 💀, 👍, 👀, 💯, 💪, ❤️ or null>\"\n"
            "  }\n"
            "]"
        )

        user_prompt = (
            f"COMMUNITY GROUP: {group_title}\n"
            f"ABOUT: {group_about}\n"
            f"DOMAIN TOPICS: {group_domain}\n\n"
            f"RECENT MESSAGES IN THIS GROUP:\n{recent_transcript}\n\n"
            f"AVAILABLE PERSONAS:\n{persona_str}\n\n"
            f"CONVERSATIONAL FOCUS: {topic_prompt}\n\n"
            f"TASK:\n"
            f"Create a natural {count}-message conversational thread for today between these personas.\n"
            f"Spread 'offset_minutes' across realistic intervals (e.g. 15, 45, 110, 240).\n"
            f"Embrace varied message lengths (ultra-short reactions, medium takes, detailed explanations), emojis, and realistic casual texting flaws. Return strictly the JSON array."
        )

        settings = {k: self.store.get(k) for k in ("fal_key", "ai_model")}
        items = []

        try:
            raw_ai, meta = await self.ai.chat_with_meta(settings, system_prompt, user_prompt, temperature=0.78)
            raw = raw_ai.strip()
            raw = re.sub(r"^```(?:json)?", "", raw).strip()
            raw = re.sub(r"```$", "", raw).strip()
            match = re.search(r"\[\s*\{.*\}\s*\]", raw, re.DOTALL)
            if match:
                clean_text = re.sub(r",\s*([\]\}])", r"\1", match.group(0))
                try:
                    items = json.loads(clean_text)
                except Exception:
                    pass
            if not items:
                for m_obj in re.finditer(r"\{[^{}]*\}", raw):
                    try:
                        clean_obj = re.sub(r",\s*([\]\}])", r"\1", m_obj.group(0))
                        items.append(json.loads(clean_obj))
                    except Exception:
                        pass
            now_ts = int(time.time())
            p_tok = meta.get("prompt_tokens", 0)
            c_tok = meta.get("completion_tokens", 0)
            tot_tok = p_tok + c_tok
            cost_est = (tot_tok / 1000.0) * 0.0003
            self.store.q("""
                INSERT INTO usage_logs(timestamp, event_type, chat_id, tokens_prompt, tokens_completion,
                                       tokens_total, latency_ms, model, cost_est)
                VALUES(?,?,?,?,?,?,?,?,?)
            """, (now_ts, "ai_batch", chat_id, p_tok, c_tok, tot_tok, meta.get("latency_ms", 0),
                  meta.get("model", ""), cost_est))
        except Exception as e:
            log.error("AI batch generation failed for group %s: %s", chat_id, e)
            raise RuntimeError(f"AI generation failed: {e}") from e

        # Enforce natural Telegram dynamics and strip any robotic name-calling across all items
        try:
            cand_names = [c.get("persona_name") or c.get("account_name") for c in candidates if c.get("persona_name") or c.get("account_name")]
            for it in items:
                if it.get("text") and not it.get("reaction"):
                    cleaned = self.ai.format_telegram_message(it["text"], names=cand_names, max_chars=360)
                    it["text"] = maybe_inject_typo_correction(cleaned, chance=0.07)
        except Exception as e:
            log.warning("Error formatting batch items: %s", e)

        # 4. Auto-clear past failed and cancelled items for this chat to keep queue fresh
        try:
            self.store.q("DELETE FROM daily_batch_items WHERE chat_id=? AND status IN ('failed', 'cancelled')", (chat_id,))
            self.store.q("DELETE FROM queue WHERE chat_id=? AND kind='batch_scheduled_send' AND status IN ('failed', 'cancelled')", (chat_id,))
        except Exception as e:
            log.debug("Auto-cleanup failed items: %s", e)

        # 4. Schedule the items throughout today
        today = self.today_str()
        cur_batch = self.store.rows("SELECT id FROM daily_batches WHERE chat_id=? AND date_str=?", (chat_id, today))
        if cur_batch:
            batch_id = cur_batch[0]["id"]
        else:
            cur = self.store.q(
                "INSERT INTO daily_batches(date_str, chat_id, topic, created_at) VALUES(?,?,?,?)",
                (today, chat_id, topic_prompt, int(time.time()))
            )
            batch_id = cur.lastrowid

        # Calculate organic clustered conversational arcs with high activity and varied rhythm
        now_ts = int(time.time())
        created_items = []

        # Round-the-clock individual sleep timing:
        # Instead of a global quiet blackout, each persona has their own sleep window and timezone.
        # Batches start immediately with tight, high-velocity conversation intervals.
        # Rapid immediate start: batch begins within 10 to 30 seconds
        start_delay_min = random.uniform(0.15, 0.45)

        # Partition items into organic clusters of 1 to 4 messages (conversational arcs)
        clusters = []
        rem_items = len(items)
        while rem_items > 0:
            c_size = min(rem_items, random.choices([1, 2, 3, 4], weights=[0.15, 0.40, 0.35, 0.10])[0])
            clusters.append(c_size)
            rem_items -= c_size

        num_clusters = len(clusters)
        # Hyper-active pacing: tight 1.0 to 2.5 minute gaps between conversation arcs
        target_cluster_gap_min = max(1.0, min(2.5, 75.0 / max(num_clusters, 1)))

        offsets_min = []
        cluster_cur_min = start_delay_min

        for c_idx, c_size in enumerate(clusters):
            if c_idx > 0:
                # Dynamic organic pacing between conversation clusters: 1.0 to 2.5 minutes
                gap = target_cluster_gap_min * random.uniform(0.80, 1.15)
                cluster_cur_min += gap

            msg_time = cluster_cur_min
            for msg_i in range(c_size):
                if msg_i > 0:
                    # Snappy rapid mobile texting interval: 8 to 22 seconds between personas
                    msg_time += random.uniform(0.13, 0.36)
                offsets_min.append(msg_time)

        cand_usage = { (c["persona_id"], c["account_id"]): 0 for c in candidates }

        for idx, item in enumerate(items):
            pid = item.get("persona_id")
            aid = item.get("account_id")
            sname = (item.get("sender_name") or "").lower()

            cand_match = [c for c in candidates if c["persona_id"] == pid and c["account_id"] == aid]
            if not cand_match and pid:
                cand_match = [c for c in candidates if c["persona_id"] == pid]
            if not cand_match and aid:
                cand_match = [c for c in candidates if c["account_id"] == aid]
            if not cand_match and sname:
                cand_match = [c for c in candidates if sname in (c.get("persona_name") or "").lower() or sname in (c.get("account_name") or "").lower()]
            if not cand_match:
                sched_m = offsets_min[idx] if idx < len(offsets_min) else (idx * 3.0)
                target_ts = now_ts + int(sched_m * 60)
                # Prioritize awake candidates who haven't spoken excessively
                awake_cands = [c for c in candidates if self.is_candidate_awake(c, ts=target_ts)]
                pool = awake_cands if awake_cands else candidates
                least_used = sorted(pool, key=lambda c: cand_usage.get((c["persona_id"], c["account_id"]), 0))[0]
                cand_match = [least_used]

            chosen_cand = cand_match[0]
            pid = chosen_cand["persona_id"]
            aid = chosen_cand["account_id"]
            sender_name = chosen_cand["persona_name"]
            cand_usage[(pid, aid)] = cand_usage.get((pid, aid), 0) + 1

            text = (item.get("text") or "").strip()
            reaction = (item.get("reaction") or "").strip()
            if not text and not reaction:
                continue
            if not text and reaction:
                text = f"[reaction: {reaction}]"
            elif text:
                text = self.ai.format_telegram_message(text, names=cand_names, max_chars=360)
                if not text:
                    continue 

            scheduled_min = offsets_min[idx] if idx < len(offsets_min) else (curr_min + (idx * 2))
            jitter_sec = random.randint(-5, 8)
            scheduled_ts = max(now_ts + 5, now_ts + int(scheduled_min * 60) + jitter_sec)

            # Determine parent_item_id for native Telegram reply quotes:
            rep_step = item.get("reply_to_step")
            parent_item_id = None
            if rep_step is not None and isinstance(rep_step, int) and 0 <= rep_step < len(created_items):
                parent_item_id = created_items[rep_step]["id"]
            elif idx > 0 and (offsets_min[idx] - offsets_min[idx-1]) < 8.0 and created_items:
                # Same conversation burst: automatically reply to previous message in cluster
                parent_item_id = created_items[-1]["id"]

            cur_item = self.store.q(
                "INSERT INTO daily_batch_items(batch_id, chat_id, account_id, persona_id, sender_name, text, scheduled_ts, status, created_at, reply_to_item_id) "
                "VALUES(?,?,?,?,?,?,?,'pending',?,?)",
                (batch_id, chat_id, aid, pid, sender_name, text, scheduled_ts, now_ts, parent_item_id)
            )
            item_id = cur_item.lastrowid

            # Enqueue into work queue for automated wake-and-send
            self.store.enqueue(
                "batch_scheduled_send",
                chat_id,
                {
                    "batch_item_id": item_id,
                    "account_id": aid,
                    "persona_id": pid,
                    "text": text,
                    "reaction": reaction if reaction else None,
                    "reply_to_item_id": parent_item_id
                },
                dedupe_key=f"batch_send:{item_id}",
                delay=max(0, scheduled_ts - now_ts)
            )

            created_items.append({
                "id": item_id,
                "persona_id": pid,
                "account_id": aid,
                "sender_name": sender_name,
                "scheduled_ts": scheduled_ts,
                "text": text,
                "status": "pending",
                "reply_to_item_id": parent_item_id
            })

        return {
            "batch_id": batch_id,
            "chat_id": chat_id,
            "count": len(created_items),
            "date": today,
            "items": created_items
        }

    def get_status(self):
        """Returns today's batch schedule, execution metrics, and scout configuration."""
        today = self.today_str()
        batches = self.store.rows("SELECT * FROM daily_batches WHERE date_str=? ORDER BY id DESC", (today,))
        all_items = self.store.rows(
            "SELECT dbi.*, g.title as group_title, p.name as persona_name, a.name as account_name "
            "FROM daily_batch_items dbi "
            "LEFT JOIN groups g ON g.chat_id=dbi.chat_id "
            "LEFT JOIN personas p ON p.id=dbi.persona_id "
            "LEFT JOIN accounts a ON a.id=dbi.account_id "
            "WHERE COALESCE(dbi.created_at, dbi.scheduled_ts, 0) >= ? "
            "ORDER BY dbi.scheduled_ts ASC",
            (int(time.time()) - 86400,)
        )

        total = len(all_items)
        pending = sum(1 for x in all_items if x["status"] == "pending")
        sent = sum(1 for x in all_items if x["status"] == "sent")
        postponed = sum(1 for x in all_items if x["status"] == "postponed")
        failed = sum(1 for x in all_items if x["status"] == "failed")
        cancelled = sum(1 for x in all_items if x["status"] == "cancelled")

        # Find next scheduled execution
        now_ts = int(time.time())
        next_items = [x for x in all_items if x["status"] == "pending" and x["scheduled_ts"] >= now_ts]
        next_ts = next_items[0]["scheduled_ts"] if next_items else None

        # Check scout account
        scout_aid = self.store.get("scout_account_id")
        scout_info = None
        if scout_aid:
            acc = self.store.rows("SELECT id, name, phone, username, active FROM accounts WHERE id=?", (scout_aid,))
            if acc:
                scout_info = acc[0]
        if not scout_info:
            acc = self.store.rows("SELECT id, name, phone, username, active FROM accounts WHERE active=1 ORDER BY id ASC LIMIT 1")
            if acc:
                scout_info = acc[0]

        auto_pilot = bool(self.store.get("daily_batch_auto_pilot", True))
        batch_count = int(self.store.get("batch_messages_count", 48))
        human_pause = max(0.5, round(int(self.store.get("batch_human_pause_seconds", 30)) / 60.0, 1))
        last_human = self.store.get("last_human_event")

        return {
            "date": today,
            "batches_count": len(batches),
            "auto_pilot": auto_pilot,
            "batch_messages_count": batch_count,
            "human_pause_minutes": human_pause,
            "session_interval_seconds": int(self.store.get("batch_session_interval_seconds", 90)),
            "last_human_event": last_human,
            "stats": {
                "total": total,
                "pending": pending,
                "sent": sent,
                "postponed": postponed,
                "failed": failed,
                "cancelled": cancelled
            },
            "next_scheduled_ts": next_ts,
            "items": all_items,
            "scout_account": scout_info,
            "live_ai_mode": "Humans Only (Bot replies strictly filtered)"
        }

    def clear_today(self, chat_id=None):
        """Cancels and purges all pending, postponed, and failed scheduled batch messages for today."""
        today = self.today_str()
        if chat_id:
            items = self.store.rows(
                "SELECT id FROM daily_batch_items WHERE chat_id=? AND status IN ('pending', 'postponed', 'failed')", (int(chat_id),)
            )
            for it in items:
                self.store.q("DELETE FROM daily_batch_items WHERE id=?", (it["id"],))
                self.store.q("DELETE FROM queue WHERE dedupe_key=?", (f"batch_send:{it['id']}",))
            self.store.q("DELETE FROM queue WHERE kind='batch_scheduled_send' AND chat_id=? AND status IN ('pending', 'postponed', 'failed')", (int(chat_id),))
        else:
            items = self.store.rows("SELECT id FROM daily_batch_items WHERE status IN ('pending', 'postponed', 'failed')")
            for it in items:
                self.store.q("DELETE FROM daily_batch_items WHERE id=?", (it["id"],))
                self.store.q("DELETE FROM queue WHERE dedupe_key=?", (f"batch_send:{it['id']}",))
            self.store.q("DELETE FROM queue WHERE kind='batch_scheduled_send' AND status IN ('pending', 'postponed', 'failed')")
        return len(items)

    def clear_failed(self, chat_id=None):
        """Specifically removes and purges all failed batch items and failed queue entries."""
        if chat_id:
            items = self.store.rows("SELECT id FROM daily_batch_items WHERE chat_id=? AND status='failed'", (int(chat_id),))
            for it in items:
                self.store.q("DELETE FROM daily_batch_items WHERE id=?", (it["id"],))
                self.store.q("DELETE FROM queue WHERE dedupe_key=?", (f"batch_send:{it['id']}",))
            self.store.q("DELETE FROM queue WHERE kind='batch_scheduled_send' AND chat_id=? AND status='failed'", (int(chat_id),))
        else:
            items = self.store.rows("SELECT id FROM daily_batch_items WHERE status='failed'")
            for it in items:
                self.store.q("DELETE FROM daily_batch_items WHERE id=?", (it["id"],))
                self.store.q("DELETE FROM queue WHERE dedupe_key=?", (f"batch_send:{it['id']}",))
            self.store.q("DELETE FROM queue WHERE status='failed'")
        return len(items)

    def trigger_next_now(self):
        """Forces the next pending or postponed scheduled message to send immediately."""
        now_ts = int(time.time())
        pending = self.store.rows(
            "SELECT id, chat_id FROM daily_batch_items WHERE status IN ('pending', 'postponed') ORDER BY scheduled_ts ASC LIMIT 1"
        )
        if not pending:
            return None
        it_id = pending[0]["id"]
        self.store.q("UPDATE daily_batch_items SET status='pending', scheduled_ts=? WHERE id=?", (now_ts, it_id))
        self.store.q("UPDATE queue SET status='pending', not_before=? WHERE dedupe_key=?", (now_ts, f"batch_send:{it_id}"))
        return it_id
