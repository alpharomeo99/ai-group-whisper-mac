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



# ======================================================================
# COMPREHENSIVE 3-SESSION CONVERSATIONAL THEME REPOSITORY (270 TOTAL THEMES)
# ======================================================================
# Session 1: Peptides, Compounds, TRT, Bloodwork & Recovery Protocols (90 themes)
SESSION_1_THEMES = [
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
    ("BPC-157 oral arginate vs subq acetate: gut healing, Crohn's/IBS flare-ups, systemic bioavailability differences.", "bpc arginate acetate oral subq gut ibs bioavailability"),
    ("TB-500 systemic migration vs local site pinning: muscle belly tears, hamstring strains, tissue remodeling.", "tb500 systemic migration local pin muscle tear hamstring strain"),
    ("GHK-Cu injection burn solutions: dilution ratios with BAC water, combining with BPC-157 in the same barrel to kill sting.", "ghk copper sting burn dilute bac water bpc barrel combo"),
    ("CJC-1295 no DAC vs DAC: half-life mechanics, pulsatile GH release vs constant somatotroph stimulation and pituitary bleed.", "cjc no dac dac half life somatotroph gh pulse pituitary"),
    ("Ipamorelin vs GHRP-2 / GHRP-6: avoiding prolactin and cortisol spikes, clean GH release without insane midnight hunger.", "ipamorelin ghrp2 ghrp6 prolactin cortisol hunger gh spike"),
    ("Epithalon (Epitalon) longevity protocol: 10-day bi-annual cycles, pineal gland reset, endogenous melatonin, deep restorative sleep.", "epithalon epitalon telomere pineal melatonin sleep longevity"),
    ("Selank & Semax intranasal dosing: acute cognitive focus during heavy workloads, BDNF stimulation, zero-jitter anxiety relief.", "selank semax intranasal bdnf cognitive focus anxiety work"),
    ("MOTS-c metabolic peptide timing: morning fasted cardio administration, fatty acid oxidation, endurance capacity, GLUT4 translocation.", "motsc mitochondria fasted cardio fat oxidation endurance glut4"),
    ("SS-31 mitochondrial repair: cardiolipin targeting, cellular ATP restoration, chronic fatigue relief, cardiac tissue recovery.", "ss31 mitochondria cardiolipin atp fatigue cellular recovery"),
    ("AOD-9604 vs Fragment 176-191: fasted morning fat burning, lipolysis without affecting blood glucose or IGF-1 spikes.", "aod9604 fragment 176 191 lipolysis fat burn fasting glucose igf1"),
    ("Tesofensine appetite suppression: dopamine and noradrenaline reuptake, laser focus, dry mouth, keeping caloric deficits effortless.", "tesofensine dopamine noradrenaline appetite deficit cut laser focus"),
    ("KPV anti-inflammatory tripeptide: gut mucosal healing, colitis relief, systemic inflammation, antimicrobial properties.", "kpv tripeptide inflammation gut colitis mucosal healing antimicrobial"),
    ("ARA-290 for peripheral neuropathy: repairing damaged nerve endings, burning nerve pain relief without raising hematocrit.", "ara290 neuropathy nerve repair burning pain rbc erythropoietin"),
    ("PT-141 (Bremelanotide) timing & experience: CNS melanocortin receptor stimulation, timing 4-6 hours prior, transient flushing and mild nausea.", "pt141 bremelanotide libido melanocortin timing nausea flush"),
    ("Melanotan II (MT-2) summer dosing: micro-dosing to avoid facial freckles, rapid tan with minimal UV, spontaneous erections.", "melanotan mt2 tan freckles uv sun libido erections microdose"),
    ("IGF-1 LR3 vs DES: pre-workout vs post-workout, intense pumps, hypoglycemia management, muscle nutrient shuttling.", "igf1 lr3 des pump hypoglycemia preworkout postworkout shuttling"),
    ("PEG-MGF (Mechano Growth Factor): localized muscle recovery after brutal eccentric workouts, satellite cell activation.", "peg mgf mechano growth factor eccentric satellite cell repair"),
    ("Thymosin Alpha-1 immune defense: warding off flu season, T-cell activation, keeping training uninterrupted while others get sick.", "thymosin alpha1 immune tcell infection illness defense flu"),
    ("Kisspeptin-10 for HPTA stimulation: restarting LH pulses, testicular fullness on TRT without HCG, timing and dosage.", "kisspeptin hpta lh pulses testes trt fertility restart"),
    ("FOXO4-DRI senolytic clearance: clearing out senescent 'zombie' cells, tissue rejuvenation, joint and skin recovery.", "foxo4 senolytic zombie cells longevity joint rejuvenation"),
    ("Reconstitution math without mistakes: 2ml vs 3ml BAC water calculations, insulin syringe tick marks, preventing wasted peptide.", "recon math bac water units iu tick marks calculation syringe"),
    ("Bacteriostatic water shelf life & acidity: benzyl alcohol stinging, checking for particulate floaters, fridge storage hygiene.", "bacteriostatic water bac shelf life benzyl alcohol sting fridge"),
    ("Needle gauges & comfort: 31g 5/16 vs 29g 1/2 inch, backloading syringes, zero dead-space needles, painless subq pinning.", "needle gauge 31g 29g backload zero dead space subq pain"),
    ("Traveling internationally with peptides: cooling travel cases, doctor notes, carry-on rules, hotel fridge temperature checks.", "travel flight cooling case carryon hotel fridge international"),
    ("Testosterone Cypionate vs Enanthate: castor oil vs cottonseed oil, ester clearance rates, allergic reactions to carrier oils.", "testosterone cypionate enanthate carrier oil castor cottonseed ester"),
    ("Sustanon 250 multi-ester debate: short-acting propionate PIP, hormonal fluctuations, why flat single-ester Cyp is smoother.", "sustanon propionate pip fluctuations single ester cypionate smooth"),
    ("Subcutaneous vs Shallow IM TRT: absorption speed, aromatization rates, avoiding subcutaneous oil nodules, delt pinning ease.", "subq shallow im trt aromatization nodules delt pin absorption"),
    ("Daily micro-dosing vs 2x weekly TRT: flattening estradiol and DHT spikes, eliminating the weekend crash, stable mood and libido.", "daily microdose 2x weekly trt stable mood crash estradiol dht"),
    ("Estrogen management & sensitive estradiol tests: LC/MS-MS vs standard ECLIA immunoassay, why ultra-sensitive labs matter.", "estrogen estradiol sensitive lcms e2 test immunoassay labs accurate"),
    ("Crashing E2 with Arimidex horror stories: dry aching joints, emotional numbness, zero libido, why Aromasin suicidal inhibition differs.", "crash e2 arimidex joints emotion libido aromasin exemestane ai"),
    ("Prolactin management on 19-nors (Deca/Tren): P5P (active B6) vs cabergoline, progesterone sensitivity, nipple sensitivity.", "prolactin 19nor deca tren p5p cabergoline progesterone nipple"),
    ("Deca vs NPP (Nandrolone Phenylpropionate): joint lubrication relief, half-life clearance speed if sides hit, deca-dick prevention.", "deca npp nandrolone joint relief half life deca dick test base"),
    ("Turinabol (Tbol) clean dry strength: zero water retention, steady endurance, liver enzymes on orals, steady progress without bloat.", "tbol turinabol dry strength endurance liver water bloat oral"),
    ("Proviron (Mesterolone) for SHBG reduction: freeing up bound testosterone, enhanced libido, hard dry cosmetic look, vascularity.", "proviron mesterolone shbg free test libido dry cosmetic look"),
    ("Equipoise (Boldenone) endurance vs RBC count: incredible cardio stamina, slow undecylenate ester, ravenous appetite, monitoring kidney values.", "equipoise eq boldenone rbc endurance stamina appetite kidneys"),
    ("Trestolone (MENT) potency: 10x more anabolic than test, rapid strength surges, heavy aromatization control, short half-life.", "ment trestolone potency strength aromatization mass short ester"),
    ("Virgin muscle PIP (Post-Injection Pain): walking like a pirate after first quad pin, why ventroglute is the king of injection sites.", "virgin muscle pip quad injection limp ventroglute pain soreness"),
    ("Preventing scar tissue buildup: rotating pin sites (delts, lats, glutes, ventroglute), massage guns, warm compresses, using 27g-29g.", "scar tissue rotation pin sites delts lats glutes ventroglute needle"),
    ("Bloodwork deep-dive: high AST/ALT caused by heavy weight training vs actual liver damage, testing GGT and CK (creatine kinase).", "bloodwork ast alt lifting muscle damage ggt ck liver labs"),
    ("eGFR and Creatinine false alarms: heavy lifting and high muscle mass elevating creatinine, why Cystatin-C is the true kidney benchmark.", "egfr creatinine false alarm muscle mass cystatin c kidney test"),
    ("Lipid management on blast: ApoB vs LDL-C, citrus bergamot 1000mg, aged garlic extract, high-EPA fish oil, preserving HDL.", "lipids apob ldl hdl citrus bergamot garlic fish oil cholesterol"),
    ("High SHBG optimization: boron 10mg daily, Tongkat Ali, carb refeeds, freeing up free testosterone when total test is high.", "shbg boron tongkat free test carbs binding globulin labs"),
    ("Blood pressure protocols for lifters: Telmisartan ARB benefits, nebivolol, daily cardio, sauna flushing, reducing left ventricular hypertrophy.", "blood pressure bp telmisartan nebivolol lvh cardio hypertension"),
    ("Acne & skin protocols: salicylic acid 2%, PanOxyl 10% benzoyl peroxide, hypochlorous acid sprays, low-dose Accutane (10-20mg/wk).", "acne benzoyl peroxide salicylic panoxyl hypochlorous accutane skin"),
    ("HCG (Human Chorionic Gonadotropin) on TRT: 250-500 IU 2-3x/week, intratesticular testosterone, maintaining Leydig cell function & fertility.", "hcg fertility leydig testicles trt sperm intratesticular"),
    ("Torn pec and shoulder rehab milestones: eccentric cable work, BPC/TB local therapy, rebuilding bench press with neutral grip dumbbells.", "pec tear rehab shoulder eccentric bench bpc tb dumbbells"),
    ("Patellar tendonitis & jumper's knee fixes: slant board eccentric squats, isometric wall sits, flossing bands, strengthening quads and hips.", "patellar tendonitis knee slant board eccentrics isometric quad"),
    ("Shoulder impingement & rotator cuff fixes: dead hangs from pull-up bars, face pulls with external rotation, rear delt activation.", "shoulder impingement rotator cuff dead hangs face pulls rear delt"),
    ("Blood Flow Restriction (BFR) training: 20-30% 1RM loads, occluding venous return, insane muscle hypertrophy without joint wear.", "bfr blood flow restriction occlusion cuffs hypertrophy joints light"),
    ("Cold plunge debate: why cold plunge right after lifting kills muscle protein synthesis, moving ice baths to rest days or morning.", "cold plunge ice bath hypertrophy mtor protein synthesis timing"),
    ("Sauna protocols for longevity: 20 minutes at 180°F, heat shock proteins, growth hormone release, cardiovascular conditioning.", "sauna heat shock proteins gh cardiovascular blood flow recovery"),
    ("Liver protection stacks: NAC (N-Acetyl Cysteine) 1200mg + TUDCA 500mg, why taking NAC before vs after alcohol/orals matters.", "nac tudca liver protection glutathione orals bile acid enzymes"),
    ("Insulin sensitivity optimization: Berberine 500mg with high-carb meals, Metformin, post-meal 10-minute walks, morning fasted cardio.", "berberine insulin sensitivity glucose metformin walks carbs postmeal"),
    ("Coronary Artery Calcium (CAC) scan: calcium scores in lifters, early plaque detection, why every lifter over 30 on gear should get one.", "cac coronary calcium scan plaque heart heart disease lifter gear"),
    ("Thyroid optimization (T3 / T4): TSH suppression during prolonged cutting deficits, thyroid reboot, maintaining basal metabolic rate.", "thyroid t3 t4 tsh cut deficit metabolic rate metabolism"),
    ("Growth Hormone (HGH) real pharma vs generic: 2-4 IU anti-aging and fat mobilization, carpal tunnel symptoms, water retention in fingers.", "hgh somatropin fat loss carpal tunnel water retention pharma iu"),
]

# Session 2: Everyday Life, Hilarious Gym Characters, Dating, Meal Prep & Banter (90 themes)
SESSION_2_THEMES = [
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
    ("Equipment hogs & gym etiquette: someone putting a towel and water bottle on 4 machines simultaneously at peak 5 PM hours.", "equipment hog towel water bottle machines etiquette peak hours rude"),
    ("The dreaded gym bathroom horror stories: walking in after leg day, people taking pre-workout dumps, no paper towels.", "bathroom locker room preworkout leg day horror funny gym"),
    ("Old gym injuries & dumb childhood stunts: breaking an arm jumping off a shed at 10 years old, dropping a 25lb plate on a toe.", "injury childhood dumb story broke bone toe dropped plate hospital"),
    ("Shoes for lifting debate: barefoot / socks vs Converse Chuck Taylors vs Vans vs expensive squat lifting shoes.", "shoes lifting converse vans barefoot squat shoes flat sole"),
    ("Pre-workout side effects: face itching from beta-alanine, heart racing, feeling like you can fight a bear in traffic.", "preworkout beta alanine tingles itch heart pump energy scoops"),
    ("Lifting belt & gear addiction: lever belt vs prong, knee sleeves smelling like biological hazard, wrist wraps.", "lever belt sbd inzer knee sleeves smell wrist wraps gear gym"),
    ("Existential gym thoughts: why do we lift heavy iron in circles just to put it back down, but we wouldn't trade it for the world.", "existential lifting why iron heavy circles life love philosophy"),
    ("The high-school broccoli-hair SARM squad: 5 kids taking turns doing half-reps on the cable station for 45 minutes while texting.", "broccoli hair teens sarm squad cable machine hog phone text gym"),
    ("The 135lb screaming bench presser: chalking hands like an Olympic lifter, yelling at the ceiling, dropping the bar onto safety pins.", "screaming bench press 135 chalk yell loud noise gym funny"),
    ("The naked locker room philosopher: 70-year-old dude holding a 15-minute geopolitical conversation while totally naked on a bench.", "locker room naked old dude conversation bench towel funny"),
    ("The tripod influencer invasion: setting up 3 tripods in the dumbbell section and glaring at people who walk in their shot.", "influencer tripod filming camera dumbbell walkway glare social media"),
    ("The dumbbell hoarder fortress: gathering 6 pairs of dumbbells around one flat bench and refusing to let anyone work in.", "dumbbell hoarder fortress bench greed gym etiquette work in"),
    ("Unsolicited quarter-squat advice: the gym veteran with knee wraps giving advice on squat depth while doing 2-inch knee bends.", "quarter squat unsolicited advice knee wraps depth ego lifting"),
    ("The treadmill winter parka runner: wearing a full heavy winter jacket and sweatpants in a 75-degree gym looking like a hostage.", "treadmill parka winter jacket sweat running sweatbox hostage"),
    ("The shadowboxer by the mirrors: throwing wild 6-punch combos between sets of lat pulldowns, dodging imaginary hooks.", "shadowboxer mirror punches combo lat pulldown imaginary fight funny"),
    ("The dropped Pyrex disaster: dropping a glass container of 2 lbs ground beef and jasmine rice on the kitchen tile at 11:30 PM.", "pyrex glass shatter tile ground beef rice midnight disaster food"),
    ("The forgotten shaker bottle in a hot car: left in July for 4 days, opening it and releasing chemical biological warfare in the garage.", "shaker bottle hot car rotten whey biological warfare smell stench"),
    ("Tuna in the corporate breakroom: heating up canned tuna in the office microwave and getting passive-aggressive emails from HR.", "tuna microwave office breakroom hr smell fish complaint coworker"),
    ("The Ninja Creami frozen concrete disaster: blade screeching, motor smoking, ending up with a solid cylinder of protein ice.", "ninja creami blade smoke concrete frozen block protein ice fail"),
    ("Running out of toilet paper on 70g fiber: the catastrophic realization after eating 3 bags of broccoli and 400g oats.", "toilet paper fiber emergency bathroom oats broccoli digestive crisis"),
    ("Air fryer grease fire with turkey bacon: smoke alarms waking the entire apartment complex up at 6 AM on a Sunday.", "airfryer fire smoke alarm turkey bacon apartment morning disaster"),
    ("Dry chicken breast choking hazard: chewing plain chicken breast until it turns into sawdust, needing 4 gulps of water per swallow.", "dry chicken sawdust choking water swallow mealprep chewing jaw"),
    ("Accidental 5,000-calorie peanut butter binge: telling yourself 'just one spoon' at midnight during week 8 of a strict cut.", "peanut butter binge midnight spoon jar calories cut deficit fail"),
    ("The food scale at a sushi restaurant: pulling out a digital scale on a first date to weigh sashimi, watching her face dissolve.", "food scale sushi date restaurant awkward weigh sashimi macros"),
    ("Girlfriend complaining about leg twitches: nervous system fried after heavy leg day, shaking the entire mattress while trying to sleep.", "leg twitch nervous system leg day mattress sleep girlfriend twitching"),
    ("Matching with a gym regular on Hinge: having an awkward date, then having to make accidental eye contact at the squat rack every Tuesday.", "hinge date gym regular awkward squat rack eye contact avoid"),
    ("Dating someone who doesn't understand lifting: 'Why do you need so much powder? Can't you just eat an apple?'.", "dating non lifter protein powder apple confusion relationship macros"),
    ("The suit jacket shoulder blowout: reaching across the dinner table at a wedding and feeling the back seam rip open.", "suit jacket rip shoulders wedding blowout formal tight bulk"),
    ("Getting caught flexing in the bathroom mirror: someone walking in while you're checking lat spread lighting under the airport sink.", "flexing bathroom mirror caught airport lighting lat spread awkward"),
    ("Falling asleep on mute in an all-hands Zoom call: waking up 20 minutes after the meeting ended with only 2 people left in the room.", "zoom call sleep mute meeting remote work camera asleep awkward"),
    ("Taking fake business calls for 10,000 steps: pacing the office hallway with a dead phone against your ear just to hit cardio goals.", "fake phone call steps hallway pacing cardio 10k steps office"),
    ("Lower back pumps in corporate office chairs: lumbar spine completely seized up after deadlifts, unable to sit down for 30 minutes.", "back pumps office chair lumbar desk standing deadlifts work"),
    ("Tradesmen roasting corporate desk workers: construction buddy laughing when you complain about mouse hand and screen fatigue.", "trades construction desk worker mouse wrist calluses roast banter"),
    ("The pre-workout double espresso commute disaster: digestive urgency hitting exactly when you get stuck in bumper-to-bumper bridge traffic.", "preworkout espresso coffee traffic commute bridge bathroom emergency"),
    ("Costco bulk haul cart dominance: 40 lbs of chicken breast, 12 cartons of egg whites, 6 tubs of Fage yogurt, cashier asking if you own a restaurant.", "costco cart chicken egg whites yogurt cashier bulk grocery restaurant"),
    ("Protein bar digestive aftermath: eating 3 fiber-heavy protein bars in an afternoon and feeling like a pressurized gas tank.", "protein bar fiber gas bloat stomach cramps afternoon snack"),
    ("Late night hunger volume hacks: eating an entire head of cabbage with hot sauce at 1 AM just to feel anything in your stomach.", "cabbage hot sauce volume eating late night hunger cut deficit"),
    ("The post-cut legendary cheat meal: whole large pizza, box of Crumbl cookies, waking up 12 lbs heavier with sausage fingers.", "crumbl cookies pizza cheat meal water weight sodium fingers bloat"),
    ("The commercial gym acoustic pop soundtrack: trying to PR an 800lb deadlift while Taylor Swift soft acoustic plays through overhead speakers.", "taylor swift acoustic pop gym music speakers deadlift pr heavy"),
    ("Lifting shoes vs Converse Chuck Taylors: why cheap canvas shoes beat 200-dollar high-tech lifting shoes every day of the week.", "converse chuck taylors lifting shoes flat sole barefoot cheap vs expensive"),
    ("Knee sleeves that smell like toxic waste: neoprene sleeves that haven't been washed in 6 months cleared out the entire power rack area.", "knee sleeves neoprene smell toxic wash odor gym bag stink"),
    ("Face itching from beta-alanine tingles: feeling like fire ants are crawling under your skin while sitting at a red light.", "beta alanine fire ants itch tingles red light face preworkout"),
    ("The dreaded gym bathroom after leg day: squatting down onto the seat with trembling quads, wondering if you'll ever stand back up.", "bathroom toilet leg day trembling quads stand up toilet struggle"),
    ("Accidental eye contact during cable flyes: locking eyes with a stranger through the mirror mid-rep and both people looking away instantly.", "cable fly mirror eye contact stranger mid rep awkward glance"),
    ("Lifting belt lever snap: lever popping open mid-valsalva maneuver with a gunshot sound that scares everyone in the weight room.", "lever belt snap gunshot valsalva pop weight room scare loud"),
    ("Working out in an unheated garage gym in January: barbell feels like dry ice, exhaling fog clouds between sets of heavy overhead press.", "garage gym winter freezing cold dry ice barbell fog breath"),
    ("Summer garage gym sauna session: lifting in 95-degree heat, sweat dripping onto the chalk tray, shirt weighing 5 lbs by set 4.", "summer garage gym heat humidity sweat chalk tray sauna session"),
    ("Chalk etiquette police: dropping half a block of gym chalk on the platform and watching the staff manager sprint over with a broom.", "chalk platform dust staff manager broom gym rules clean"),
    ("Accidentally wearing gym clothes inside out: hitting 4 sets of heavy incline dumbbell press before realizing the tag is sticking out on your chest.", "inside out gym shirt tag chest dumbbell press wardrobe fail"),
    ("Pre-workout powder dry-scoop disaster: inhaling half a scoop into the windpipe, coughing an orange powder cloud across the gym parking lot.", "dry scoop windpipe cough orange powder cloud preworkout parking lot"),
    ("The 4 PM Sunday meal prep existential crisis: staring at 6 identical glass containers of ground beef, wondering what decisions led here.", "sunday mealprep glass containers ground beef existential crisis rice routine"),
    ("Dating a vegan while eating 1.5g protein per lb: navigating restaurant menus where one person eats tofu and the other orders two 12oz ribeyes.", "vegan dating steak ribeye protein macros restaurant menu clash"),
    ("Leaving your headphones at home: the pure despair of walking into the gym, realizing you forgot your AirPods, and having to lift in silence.", "forgot headphones airpods gym silence acoustic pop despair lift"),
    ("The 5 AM alarm battle: staring at the ceiling in pitch-black darkness negotiating with your soul whether today is a rest day.", "5am alarm morning pitch black rest day snooze mental battle bed"),
    ("Buying gym clothes in large vs XL: tight sleeves cutting off circulation vs looking like a kid wearing their dad's oversized shirt.", "gym clothes tight sleeves oversized pump cover sizing fit dilemma"),
    ("Spilling an entire 5lb tub of vanilla whey powder: a white nuclear dust cloud settling over the dark kitchen floorboards and cat.", "whey powder spill floor 5lb tub kitchen mess dust cloud clean"),
    ("Accidentally loading uneven weights on the barbell: hitting 3 reps of bench press wondering why the left shoulder hurts before seeing a 35 instead of a 25.", "uneven weight barbell bench 35 25 plates mistake shoulder balance"),
]

# Session 3: Pop Culture, UFC Fight Recaps, Sports, Crypto/Tech & Late-Night Thoughts (90 themes)
SESSION_3_THEMES = [
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
    ("Stock market & inflation complaints: grocery store shocks, ribeye steak prices, cost of living.", "stocks inflation eggs groceries meat steak cost economy"),
    ("Crazy viral internet news: bizarre Florida man headlines, wild viral TikTok trends, unbelievable real-world stories.", "viral news florida man crazy headlines internet story"),
    ("Artificial Intelligence & humanoid robots: Boston Dynamics backflips, ChatGPT replacing office tasks, robot anxiety.", "ai tech robot openai chatgpt future humanoid automation"),
    ("Blockbuster movies & binge TV shows: new cinema releases, sci-fi series, HBO dramas, what people are watching during cardio.", "movies show hbo cinema netflix watch cardio series"),
    ("Severe weather extremes: heatwaves melting asphalt, blizzard snowed-in gym cancellations, working out in unheated garages.", "weather storm heatwave blizzard winter garage gym freeze"),
    ("Music releases & gym playlist culture: new album drops, death metal vs phonk vs 90s hip hop for hitting heavy PRs.", "music album playlist phonk metal hip hop beats gym"),
    ("Viral TikTok gym cringe & public filming: influencers setting up tripods in walkways, getting mad when someone walks by.", "tiktok cringe tripod filming walkway gym public etiquette"),
    ("Gaming & late night PC sessions: new game releases, staying up till 3 AM gaming while on a cut, discord banter.", "gaming pc steam console late night game release setup"),
    ("Random 2 AM shower thoughts: could a silverback gorilla bench press 1,500 lbs, how many 5th graders could you fight off.", "shower thoughts gorilla bench 2am funny debate silly thoughts"),
    ("Zombie apocalypse gym squad: picking 3 members of the group chat for the survival team based on physical attributes.", "zombie apocalypse squad team survival bunker gym group chat"),
    ("Unserious food debates: is a hot dog technically a sandwich, pineapple on pizza, ranking fast food french fries.", "hot dog sandwich debate pineapple pizza fries fast food stupid"),
    ("Apple Watch & fitness tracker obsession: closing rings, checking sleep tracking scores, obsessing over resting heart rate.", "apple watch whoop oura sleep score rings tracker steps heart"),
    ("Alex Pereira's terrifying left hook & stone face: Chama energy, knocking out light heavyweights without blinking, heavyweight move?", "alex pereira chama ufc knockout left hook poatan light heavyweight"),
    ("Jon Jones heavyweight legacy vs Tom Aspinall: the ducking accusations, legacy preservation, undisputed champion debate.", "jon jones aspinall heavyweight ufc undisputed belt ducking legacy"),
    ("Ilia Topuria knockout power & aura: cleaning out featherweight legends, pinpoint boxing, UFC stardom trajectory.", "topuria featherweight ufc boxing knockout volkanovski holloway aura"),
    ("Islam Makhachev & Dagestani wrestling dominance: handcuffs on the ground, cardio engine, pound-for-pound king debate.", "makhachev dagestani wrestling smesh ufc p4p sambo handcuffs"),
    ("BMF belt spectacles & last-second knockouts: Max Holloway pointing to the center of the cage against Gaethje at UFC 300.", "bmf belt holloway gaethje ufc 300 last second knockout center cage"),
    ("Boxing judging robberies: three blind judges scoring a clean boxing clinic as a split draw, corruption in combat sports.", "boxing judges robbery split decision scorecard corruption combat"),
    ("BKFC (Bare Knuckle Fighting Championship) chaos: brutal cuts, tooth loss, raw grit vs refined sweet science.", "bkfc bare knuckle cuts teeth blood grit combat fight sport"),
    ("Hugh Jackman Wolverine physique over 25 years: 1999 soft look vs 2024 veiny road map, dehydration and lighting secrets.", "hugh jackman wolverine 25 years veins dehydration lighting movie secret"),
    ("Kumail Nanjiani Eternals transformation memes: vascular jawline, Hollywood trainers claiming 'just chicken and broccoli'.", "kumail nanjiani jawline chicken broccoli marvel hollywood trainer meme"),
    ("Zac Efron Baywatch toll & rebound: open discussions about water pills, zero carb insomnia, and never wanting to look like that again.", "zac efron baywatch water pills insomnia rebound body dysmorphia"),
    ("The Rock 'just cod and 4 AM workouts' meme: Joe Rogan and internet calling for transparency, public skepticism.", "the rock dwayne johnson cod 4am lie transparency rogan gym meme"),
    ("Joe Rogan podcast obsessions: cold plunges, elk meat, stem cell trips to Colombia, bow hunting, reverse hyper machines.", "rogan elk meat cold plunge stem cells colombia bow hunting reverse hyper"),
    ("Derek MPMD nostalgia & breakdown classics: 'supraphysiological amounts', gorilla mind empire, breaking down fake natties.", "derek mpmd supraphysiological gorilla mind fake natty youtube breakdown"),
    ("Sam Sulek phenomenon: gallon of chocolate milk, Krispy Kreme pre-workout, heavy lifting style, relaxed cardio bike chats.", "sam sulek chocolate milk krispy kreme cardio bike youtube chats lifting"),
    ("Mike O'Hearn duck eggs & hard work memes: Baby Don't Hurt Me soundtrack, 4 decades of peak condition, hilarious internet lore.", "mike ohearn baby dont hurt me duck eggs natty meme 4 decades longevity"),
    ("Alan Ritchson (Reacher) refreshing TRT honesty: talking openly about fatigue, shoulder surgeries, and how TRT saved his career.", "alan ritchson reacher trt fatigue shoulder surgery career honesty amazon"),
    ("Mark Wahlberg 3:30 AM routine madness: cryo chamber, 4 AM golf, 2 workouts, whether anyone actually lives like that or it's PR.", "mark wahlberg 330 am routine cryo golf workouts celebrity pr schedule"),
    ("Hollywood Ozempic face red carpet epidemic: sudden 30lb weight drops, buccal fat removal, celebrities denying GLP-1s.", "ozempic face buccal fat red carpet celebrity weight loss glp1 denial"),
    ("Chris Bumstead (CBum) Classic Physique dominance: the vacuum pose, retiring at the top, passing the torch to the new era.", "cbum chris bumstead classic physique vacuum pose retirement olympia"),
    ("Eddie Hall vs Thor Bjornsson 500kg deadlift history: blood shooting from nose, years of beef, boxing match conclusion.", "eddie hall thor bjornsson 500kg deadlift nose bleed strongman beef"),
    ("Premier League & Champions League football madness: 98th-minute bicycle kick winners, VAR controversies, title race tension.", "premier league champions league bicycle kick var extra time title drama"),
    ("NFL bone-crushing tackles & fantasy league heartbreaks: star running back tearing an ACL in the 1st quarter of championship week.", "nfl fantasy football acl injury championship week heartbreak tackles"),
    ("Formula 1 race weekend chaos: tire pit-stop disasters, qualifying speed, Max Verstappen sim racing till 4 AM before a GP.", "formula 1 f1 pitstop disaster verstappen sim racing grand prix radio"),
    ("Bitcoin overnight volatility: waking up +15% feeling like Warren Buffett vs waking up -20% eating ramen, memecoin madness.", "bitcoin btc crypto ramen warren buffett volatility memecoin wallet"),
    ("AI humanoid robots doing gymnastics: Boston Dynamics Atlas doing flips, robots taking physical jobs, AI anxiety vs hype.", "boston dynamics atlas flips humanoid robots automation ai jobs future"),
    ("Apple Vision Pro headset zombies: people wearing spatial computing goggles in coffee shops looking like sci-fi characters.", "apple vision pro spatial headset coffee shop sci fi zombie gadgets"),
    ("Late-night Discord gaming sessions: playing tactical shooters until 3 AM with the boys, laughing so hard you get a headache.", "discord gaming tactical shooter 3am boys laughter headache night"),
    ("Could an average gym bro survive 30 seconds with a chimpanzee? The definitive biology debate on ape muscle density.", "chimpanzee ape muscle density gym bro 30 seconds survival fight debate"),
    ("Why commercial gyms play soft 2014 acoustic pop during 800lb deadlifts: the psychological torture of elevator music.", "commercial gym acoustic pop deadlift elevator music psychological torture"),
    ("Zombie apocalypse group chat ranking: picking which friend gets sacrificed first based on their cardio and sprint speed.", "zombie apocalypse sacrifice friend cardio sprint speed survival ranking"),
    ("Unserious food debate: is a hot dog a taco, a sandwich, or an open sub? Blood-feud intensity over useless definitions.", "hot dog taco sandwich open sub food debate useless argument"),
    ("Ranking fast food french fries with life-or-death seriousness: McDonald's fresh out of the fryer vs Wendy's sea salt vs Five Guys grease.", "french fries mcdonalds wendys five guys grease ranking tier list fast food"),
    ("Would you rather have 20-inch arms forever or unlimited free international first-class flights? The ultimate bro dilemma.", "20 inch arms free flights first class bro dilemma hypothetical choice"),
    ("Weird Florida man news headlines: riding an alligator through a drive-thru, fighting a hurricane with a sword.", "florida man alligator drive thru hurricane sword news weird headlines"),
    ("Movies we watch exclusively during cardio: fast-paced action films that make 45 minutes of stairmaster feel like 10 minutes.", "cardio movies action films stairmaster 45 min pass time fast watch"),
    ("The eternal cereal ranking tier list: Cinnamon Toast Crunch S-tier supremacy vs wet cardboard fiber cereals.", "cinnamon toast crunch cereal tier list cardboard fiber breakfast ranking"),
    ("Sleep paralysis demons vs heavy squat nightmares: waking up in a cold sweat dreaming the barbell rolled down your neck.", "sleep paralysis demon squat nightmare barbell neck cold sweat waking up"),
    ("Ancient Roman gladiators vs modern gym lifters: who would win in hand-to-hand combat, diet differences, barley porridge vs whey protein.", "roman gladiators modern lifters combat barley porridge whey protein fight"),
    ("The mystery of the single lost AirPod: searching gym crevices, under weight benches, knowing it's gone to another dimension.", "lost airpod gym bench crevice dimension one earphone missing"),
    ("Sci-fi movie dystopias vs reality: flying cars vs just having 14 different streaming subscriptions and subscription heated seats in cars.", "scifi dystopia flying cars streaming subscriptions heated seat fee reality"),
    ("Fast and Furious physics laws: Vin Diesel shifting gears 19 times in a single 1/4 mile drag race, family memes.", "fast furious vin diesel 19 gears quarter mile family physics meme car"),
    ("The absurdity of modern luxury watches: spending 20k on an automatic watch that loses 4 seconds a day while a 15 Casio is atomic.", "luxury watch rolex casio atomic time 20k vs 15 loss seconds funny"),
    ("Bizarre historical facts that sound fake: Napoleon being attacked by thousands of bunnies, the Great Emu War in Australia.", "historical facts napoleon bunnies emu war australia weird trivia fake"),
    ("Gaming chair ergonomics scam: bucket seats designed for race cars in a stationary bedroom causing catastrophic posture.", "gaming chair bucket seat ergonomics posture scam office chair racecar"),
    ("Coffee brewing snobbery vs instant coffee: measuring water temp to the decimal point vs dumping Nescafé into cold water at 5 AM.", "coffee snob pour over scale instant nescafe 5am caffeine debate"),
    ("The illusion of free time on weekends: waking up Saturday with 15 planned tasks and suddenly it's 8 PM Sunday night.", "weekend illusion free time saturday sunday night chores vanishing week"),
    ("Unsolved ocean mysteries: giant squids, Mariana Trench depths, why we know more about outer space than the bottom of the sea.", "ocean mariana trench giant squid deep sea space mystery unexplored"),
    ("The psychology of driving with loud bass: vibrating the entire rearview mirror at traffic lights with dark phonk or 90s hip hop.", "car bass phonk subwoofer rearview mirror vibration traffic lights music"),
    ("Extinct prehistoric megafauna: Megalodon sharks, Titanoboa giant snakes, terror birds, and whether humanity would survive them today.", "megalodon titanoboa terror birds prehistoric megafauna survive monsters"),
    ("Conspiracy theories that are hilarious: the moon landing staged by Stanley Kubrick, simulation glitches, birds aren't real memes.", "conspiracy theory kubrick moon landing simulation birds arent real meme"),
    ("Why adult friendships are so hard: trying to coordinate a dinner with 3 friends taking 6 weeks of calendar negotiations.", "adult friendships dinner schedule calendar weeks busy life friends"),
    ("The satisfaction of a clean workspace: perfectly cable-managed desk setup lasting exactly 14 minutes before wires take over.", "cable management desk clean setup wires clutter workspace tech"),
    ("The paradox of resting heart rate: being thrilled your Apple Watch says 46 bpm while the doctor asks if you're passing out.", "resting heart rate 46 bpm bradycardia apple watch doctor athlete funny"),
    ("Nostalgia for early 2000s internet: MSN messenger nudges, dial-up screech, custom forum signatures with animated GIFs.", "early 2000s internet msn messenger dialup forums signatures nostalgia"),
    ("The greatest video game intros of all time: Skyrim carriage ride, Halo Gregorian chant, GTA San Andreas opening bike ride.", "skyrim hey you youre finally awake halo chant gta san andreas game intro"),
    ("Why every dad becomes obsessed with lawn care and WWII documentaries the exact day they turn 40.", "dad lawn care grass wwii history documentary 40 birthday phenomenon"),
]

DEFAULT_DAILY_THEMES = SESSION_1_THEMES + SESSION_2_THEMES + SESSION_3_THEMES

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

        default_themes = DEFAULT_DAILY_THEMES
        fresh_themes = [
            (t, q) for (t, q) in default_themes
            if not any(q in ut.lower() or t[:25].lower() in ut.lower() for ut in used_past_topics)
        ]
        if not fresh_themes:
            fresh_themes = default_themes

        # 2C. Determine micro-threads plan: split batches into 3 focused micro-threads (Session 1, Session 2, Session 3)
        if topic_override or count < 15:
            micro_plans = [(topic_override, count)]
        elif count >= 21:
            base = count // 3
            rem = count - (base * 3)
            micro_plans = [(None, base + (1 if i < rem else 0)) for i in range(3)]
        else:
            base = count // 2
            rem = count - (base * 2)
            micro_plans = [(None, base + (1 if i < rem else 0)) for i in range(2)]

        domain_compounds = [t for t in fresh_themes if t in SESSION_1_THEMES] or SESSION_1_THEMES
        domain_banter = [t for t in fresh_themes if t in SESSION_2_THEMES] or SESSION_2_THEMES
        domain_culture = [t for t in fresh_themes if t in SESSION_3_THEMES] or SESSION_3_THEMES

        thread_configs = []
        chosen_tuples = []
        for p_i, (t_override, p_count) in enumerate(micro_plans):
            if t_override:
                thread_configs.append((t_override, t_override, p_count))
            elif len(micro_plans) == 3:
                if p_i == 0:
                    avail = [t for t in domain_compounds if t not in chosen_tuples] or SESSION_1_THEMES
                elif p_i == 1:
                    avail = [t for t in domain_banter if t not in chosen_tuples] or SESSION_2_THEMES
                else:
                    avail = [t for t in domain_culture if t not in chosen_tuples] or SESSION_3_THEMES
                chosen_t = random.choice(avail)
                chosen_tuples.append(chosen_t)
                thread_configs.append((chosen_t[0], chosen_t[1], p_count))
            elif len(micro_plans) == 2:
                if p_i == 0:
                    avail = [t for t in domain_compounds if t not in chosen_tuples] or SESSION_1_THEMES
                else:
                    avail = [t for t in (domain_banter + domain_culture) if t not in chosen_tuples] or fresh_themes
                chosen_t = random.choice(avail)
                chosen_tuples.append(chosen_t)
                thread_configs.append((chosen_t[0], chosen_t[1], p_count))
            else:
                chosen_t = random.choice(fresh_themes)
                chosen_tuples.append(chosen_t)
                thread_configs.append((chosen_t[0], chosen_t[1], p_count))
        # 3. Auto-clear past failed and cancelled items for this chat to keep queue fresh
        try:
            self.store.q("DELETE FROM daily_batch_items WHERE chat_id=? AND status IN ('failed', 'cancelled')", (chat_id,))
            self.store.q("DELETE FROM queue WHERE chat_id=? AND kind='batch_scheduled_send' AND status IN ('failed', 'cancelled')", (chat_id,))
        except Exception as e:
            log.debug("Auto-cleanup failed items: %s", e)

        # 4. Prepare daily_batches row with combined topics summary
        today = self.today_str()
        topic_summary = " | ".join(f"[Session {i+1}] {tc[0][:50]}" for i, tc in enumerate(thread_configs))
        cur_batch = self.store.rows("SELECT id FROM daily_batches WHERE chat_id=? AND date_str=?", (chat_id, today))
        if cur_batch:
            batch_id = cur_batch[0]["id"]
            self.store.q("UPDATE daily_batches SET topic=? WHERE id=?", (topic_summary, batch_id))
        else:
            cur = self.store.q(
                "INSERT INTO daily_batches(date_str, chat_id, topic, created_at) VALUES(?,?,?,?)",
                (today, chat_id, topic_summary, int(time.time()))
            )
            batch_id = cur.lastrowid

        cand_names = [c.get("persona_name") or c.get("account_name") for c in candidates if c.get("persona_name") or c.get("account_name")]
        group_title = group.get("title") or f"Group {chat_id}"
        group_about = group.get("about") or "Telegram community discussion"
        group_domain = group.get("domain_knowledge") or "Lifting, gear, TRT, SARMs, peptides, nootropics, health, celebrity transformations & pop culture, sports & fight nights, breaking news, crypto/tech, work/dating, plus truly random everyday banter."
        settings = {k: self.store.get(k) for k in ("fal_key", "ai_model")}

        sessions_items = []
        for th_idx, (th_theme, th_query, th_count) in enumerate(thread_configs):
            domain_labels = [
                "Session 1 Domain [Peptides, Compounds, TRT, Bloodwork & Recovery Protocols]",
                "Session 2 Domain [Everyday Life, Hilarious Gym Characters, Dating, Meal Prep & Banter]",
                "Session 3 Domain [Pop Culture, UFC Fight Recaps, Sports, Crypto/Tech & Late-Night Thoughts]",
            ]
            s_label = domain_labels[th_idx] if th_idx < len(domain_labels) else f"Session {th_idx + 1}"
            if th_idx == 0 and transcript_lines and random.random() < 0.25:
                recent_snippet = " ".join(m.get("text", "") for m in recent_msgs[-3:] if m.get("text"))[:140]
                topic_prompt = (
                    f"Natural conversational transition for {s_label}: starting from a quick casual nod to recent chatter ('{recent_snippet}'), "
                    f"then immediately pivoting to a fresh angle on: {th_theme}. NEVER repeat previous points."
                )
            else:
                topic_prompt = f"Focus for {s_label}: {th_theme}. One persona starts with a casual observation, funny take, real experience, or practical question."

            persona_summaries = []
            for c in candidates:
                dt = {}
                try:
                    dt = json.loads(c.get("details") or "{}") if isinstance(c.get("details"), str) else (c.get("details") or {})
                except Exception:
                    dt = {}
                role = dt.get("role_in_group") or dt.get("tone_and_vibe") or "Experienced regular"
                emojis = dt.get("signature_emojis") or "💀, 😂"
                emoji_habit = dt.get("emoji_habit") or "moderate"
                typing = dt.get("typing_style") or "casual phone texting"

                mem_lines = []
                if self.memory:
                    mem_ctx = self.memory.get_prompt_memory_context(c["persona_id"], chat_id=chat_id, limit=6, query_text=th_query)
                    if mem_ctx:
                        mem_lines.append(mem_ctx)
                elif hasattr(self.store, "rows"):
                    m_rows = self.store.rows(
                        "SELECT content FROM persona_memories WHERE persona_id=? AND (expires_at IS NULL OR expires_at > ?) "
                        "ORDER BY CASE retention_tier WHEN 'long' THEN 1 ELSE 2 END, salience DESC LIMIT 3",
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
                    c_temperament = "RAW & LOW-PATIENCE INTERNET VERNACULAR (Zero filter, roasts dumb takes/questions, cuts through nonsense with sharp dismissive internet slang)"
                elif c_tier == "dry_sarcastic":
                    c_temperament = "DRY & SKEPTICAL OBSERVER (Deadpan, rolls eyes at hype, weary one-liners, sarcastic reality checks)"
                else:
                    c_temperament = "CHILL & GROUNDED PRACTICAL PEER (Calm, knowledgeable, explains practical details without corporate tone)"

                persona_summaries.append(
                    f"- ID: {c['persona_id']}, AccountID: {c['account_id']}, Name: {c['persona_name']}\n"
                    f"  Vibe/Role: {role}\n"
                    f"  Temperament & Speech Profile: [{c_tier.upper()}] - {c_temperament}\n"
                    f"  Signature Emojis: {emojis} (habit: {emoji_habit})\n"
                    f"  Typing: {typing}\n"
                    f"  Brief: {c.get('prompt', '')[:140]}...{mem_str}"
                )
                proto_card = self.store.get_persona_protocol_card(c.get('persona_id'))
                if proto_card:
                    persona_summaries[-1] += chr(10) + '  ' + str(proto_card)

            persona_str = "\n\n".join(persona_summaries)
            rel_graph = format_relationship_graph(candidates)
            persona_str += f"\n\nINTER-PERSONA RELATIONSHIP DYNAMICS:\n{rel_graph}\n(Personas banter, reference shared history, and chat like close peers without using @tags!)"

            system_prompt = (
                "You orchestrate realistic, organic human conversations in specialist Telegram communities.\n"
                "You write authentic, casual group chat exchanges between real people texting from their phones.\n\n"
                "CRITICAL GUIDELINES FOR AUTHENTIC HUMAN MESSAGING:\n"
                "AUTHENTIC CONVERSATIONAL FRICTION & ASYMMETRIC TEMPERAMENTS (MANDATORY):\n"
                "- Group chats are NEVER unanimous or polite corporate meetings! Members have completely different social temperaments:\n"
                "  * LOW-PATIENCE / RAW VERNACULAR PERSONAS: When they speak, they have zero corporate filter and low patience. Use dismissive sharp internet vernacular ('bro what are you even saying rn', 'who told you that garbage💀', 'lol nah', 'stop overcomplicating basic shit and just train').\n"
                "  * DRY / SARCASTIC PERSONAS: Deadpan, cynical one-liners, rolling eyes at hype ('sure bro whatever you say', 'rip your sleep schedule', 'fascinating theory💀').\n"
                "  * CHILL / GROUNDED PERSONAS: Calm, experienced, give straightforward practical answers without getting heated.\n"
                "- This asymmetrical friction is what makes the group feel authentic and alive!\n\n"
                "CRITICAL: ABSOLUTE ZERO REPETITION MANDATE (STRICTLY ENFORCED):\n"
                f"{forbidden_block}\n"
                "- Do NOT re-ask questions that were already discussed.\n"
                "- Introduce 100% fresh angles, new exercises, novel questions, or everyday banter.\n\n"
                "TELEGRAM NATIVE EMOJI REACTIONS (15-25% OF TURNS):\n"
                "- Real people frequently react with emojis (🔥, 😂, 💀, 👍, 👀, 💯, 💪, ❤️) instead of typing text!\n"
                "- Set: \"reaction\": \"🔥\", \"reply_to_step\": <target step number>, \"text\": \"\"\n\n"
                "1. WILDLY ASYMMETRICAL LENGTH DIVERSITY:\n"
                "   - ULTRA-SHORT REACTIONS (25-30% of turns): 1 to 4 words ('lol nah', 'fr tho💀', 'wait what?', 'bro😭', 'same honestly').\n"
                "   - QUICK TAKES & QUESTIONS (40-45% of turns): 5 to 14 words.\n"
                "   - DETAILED REPLIES (25-30% of turns): 18 to 45 words.\n"
                "   - MULTI-MESSAGE BURSTS: Mobile texters often send 2 consecutive bubbles separated with ' ||| '.\n\n"
                "2. GENUINE TELEGRAM REPLIES & THREADING:\n"
                "   - When responding or bantering, specify 'reply_to_step': <0-indexed step of target message in this session>.\n"
                "   - If starting a new point or opening remark, set 'reply_to_step': null.\n\n"
                "3. ZERO @TAGS OR NAMES (STRICT): Never type @handles or call people by name. Address someone ONLY by setting reply_to_step.\n\n"
                "4. OUTPUT FORMAT: Return ONLY a valid JSON array of message objects matching this exact format:\n"
                "[\n"
                "  {\n"
                "    \"persona_id\": <int>,\n"
                "    \"account_id\": <int>,\n"
                "    \"sender_name\": \"<string>\",\n"
                "    \"reply_to_step\": <int or null>,\n"
                "    \"text\": \"<exact telegram message text>\",\n"
                "    \"reaction\": \"<optional emoji like 🔥, 😂, 💀, 👍, 👀, 💯, 💪, ❤️ or null>\"\n"
                "  }\n"
                "]"
            )

            user_prompt = (
                f"COMMUNITY GROUP: {group_title}\n"
                f"ABOUT: {group_about}\n"
                f"DOMAIN TOPICS: {group_domain}\n\n"
                f"AVAILABLE PERSONAS:\n{persona_str}\n\n"
                f"CONVERSATIONAL FOCUS: {topic_prompt}\n\n"
                f"TASK:\n"
                f"Create a natural {th_count}-message conversational thread for this session between these personas.\n"
                f"Ensure every persona speaks, vary message lengths wildly, and embrace casual mobile texting flaws. Return strictly the JSON array."
            )

            th_items = []
            try:
                raw_ai, meta = await self.ai.chat_with_meta(settings, system_prompt, user_prompt, temperature=0.78)
                raw = raw_ai.strip()
                raw = re.sub(r"^```(?:json)?", "", raw).strip()
                raw = re.sub(r"```$", "", raw).strip()
                match = re.search(r"\[\s*\{.*\}\s*\]", raw, re.DOTALL)
                if match:
                    clean_text = re.sub(r",\s*([\]\}])", r"\1", match.group(0))
                    try:
                        th_items = json.loads(clean_text)
                    except Exception:
                        pass
                if not th_items:
                    for m_obj in re.finditer(r"\{[^{}]*\}", raw):
                        try:
                            clean_obj = re.sub(r",\s*([\]\}])", r"\1", m_obj.group(0))
                            th_items.append(json.loads(clean_obj))
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
                log.error("AI batch generation error for session %d in group %s: %s", th_idx + 1, chat_id, e)

            if th_items:
                try:
                    for it in th_items:
                        if it.get("text") and not it.get("reaction"):
                            cleaned = self.ai.format_telegram_message(it["text"], names=cand_names, max_chars=360)
                            it["text"] = maybe_inject_typo_correction(cleaned, chance=0.07)
                except Exception as e:
                    log.warning("Formatting error in session %d: %s", th_idx + 1, e)
                sessions_items.append(th_items)

        now_ts = int(time.time())
        created_items = []
        cand_usage = { (c["persona_id"], c["account_id"]): 0 for c in candidates }

        session_start_min = random.uniform(0.15, 0.45)
        for s_idx, s_items in enumerate(sessions_items):
            clusters = []
            rem = len(s_items)
            while rem > 0:
                c_size = min(rem, random.choices([1, 2, 3, 4], weights=[0.15, 0.40, 0.35, 0.10])[0])
                clusters.append(c_size)
                rem -= c_size

            target_cluster_gap_min = max(1.0, min(2.5, 45.0 / max(len(clusters), 1)))
            session_offsets = []
            cur_time = session_start_min

            for c_idx, c_size in enumerate(clusters):
                if c_idx > 0:
                    gap = target_cluster_gap_min * random.uniform(0.80, 1.15)
                    cur_time += gap
                m_time = cur_time
                for msg_i in range(c_size):
                    if msg_i > 0:
                        m_time += random.uniform(0.13, 0.36)
                    session_offsets.append(m_time)

            session_created = []
            for item_idx, item in enumerate(s_items):
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
                    sched_m = session_offsets[item_idx] if item_idx < len(session_offsets) else (item_idx * 3.0)
                    target_ts = now_ts + int(sched_m * 60)
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

                scheduled_min = session_offsets[item_idx] if item_idx < len(session_offsets) else (session_start_min + (item_idx * 2))
                jitter_sec = random.randint(-5, 8)
                scheduled_ts = max(now_ts + 5, now_ts + int(scheduled_min * 60) + jitter_sec)

                rep_step = item.get("reply_to_step")
                parent_item_id = None
                if rep_step is not None and isinstance(rep_step, int) and 0 <= rep_step < len(session_created):
                    parent_item_id = session_created[rep_step]["id"]
                elif item_idx > 0 and (session_offsets[item_idx] - session_offsets[item_idx-1]) < 8.0 and session_created:
                    parent_item_id = session_created[-1]["id"]

                cur_item = self.store.q(
                    "INSERT INTO daily_batch_items(batch_id, chat_id, account_id, persona_id, sender_name, text, scheduled_ts, status, created_at, reply_to_item_id) "
                    "VALUES(?,?,?,?,?,?,?,'pending',?,?)",
                    (batch_id, chat_id, aid, pid, sender_name, text, scheduled_ts, now_ts, parent_item_id)
                )
                item_id = cur_item.lastrowid

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

                created_item_dict = {
                    "id": item_id,
                    "persona_id": pid,
                    "account_id": aid,
                    "sender_name": sender_name,
                    "scheduled_ts": scheduled_ts,
                    "text": text,
                    "status": "pending",
                    "reply_to_item_id": parent_item_id
                }
                session_created.append(created_item_dict)
                created_items.append(created_item_dict)

            last_session_end = session_offsets[-1] if session_offsets else session_start_min
            if len(sessions_items) >= 3:
                session_start_min = last_session_end + random.uniform(45.0, 75.0)
            else:
                session_start_min = last_session_end + random.uniform(60.0, 100.0)

        if not created_items:
            raise RuntimeError("Failed to generate any valid messages across batch sessions")

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
