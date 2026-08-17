"""Retrieval-first answering for the assistant.

The assistant used to send a patient's message straight to the provider. For a
medical question that is answering from training data — the exact failure the
knowledge base exists to prevent. This module puts retrieval in front:

    message -> is this general medical knowledge?
            -> retrieve from the approved corpus
            -> grounded answer with citations, or a safe refusal

**Nothing here is new machinery.** It decides *whether* to ground and hands off
to :mod:`knowledge.rag.service`, which already owns retrieval, context building,
the prompt, generation through the LLM facade, validation and citation checks.

Three rules shape the routing:

**A medical question never falls through to an ungrounded answer.** If the
corpus cannot support one — nothing relevant, index unreadable, provider down —
the assistant says so. It does not quietly ask the model to improvise, which is
what "fail safely" has to mean to be worth anything.

**Grounded answers carry no patient data.** The knowledge path builds its
context from published medical reference material only. The user's record is
assembled on the *other* branch, for questions that are actually about them.
Mixing the two is a future task with its own authorisation work.

**Personal and logistical questions are left alone.** "When is my appointment"
is not a knowledge question, and routing it into the corpus would answer a
working feature with "I couldn't find that in the medical sources."
"""
import logging
import os
import re

logger = logging.getLogger("appointments")

#: ``on``   — always ground medical questions, even with an empty corpus.
#: ``off``  — never ground; the assistant behaves as it did before.
#: ``auto`` — ground when the knowledge base actually has retrievable documents.
#:            The default, because a deployment that has not ingested a corpus
#:            should not have every medical question answered with "I found
#:            nothing", and an empty corpus is a deployment state rather than a
#:            retrieval failure.
GROUNDING_VAR = "AI_GROUNDING"

_WORD = r"(?:^|\W)"

#: Questions Roshada answers from its **own data** — the caller's record, the
#: provider directory, using the product. These go to the tool-using assistant,
#: never to the knowledge base: "what are my medicines" is answered by looking
#: them up, and a knowledge-base miss would refuse a question the platform can
#: answer perfectly well.
#:
#: Checked first, and specific on purpose: a bare "my" is not enough, because
#: "what should I do about my blood pressure" is a knowledge question that
#: happens to be personal.
_PERSONAL = re.compile(
    _WORD + "(?:"
    r"my (?:appointment|appointments|booking|bookings|schedule|prescription|"
    r"prescriptions|medicine|medicines|medication|medications|drugs|"
    r"results?|report|reports|record|records|"
    r"file|profile|doctor|doctors|pharmacy|order|orders|account|password|"
    r"patient|patients)"
    r"|when is my|do i have (?:an |any )?appointment"
    r"|book (?:an |a )?(?:appointment|visit|test|scan)"
    r"|(?:re-?schedule|cancel) (?:my |an |the )?"
    r"(?:appointment|booking|visit|order)"
    r"|log ?in|sign ?up|reset my password"
    # Looking things up in Roshada rather than asking about medicine.
    r"|available doctors?|doctors? available|which doctors?|what doctors?"
    r"|find (?:me )?a doctor|who (?:are|is) (?:the )?doctors?"
    r"|booked with me|patients? (?:do i have|of mine)"
    # Arabic: my appointment(s), my prescription, my medicines, my results,
    # my doctor, my file, book, cancel, reschedule, which doctors are
    # available, who booked with me.
    "|موعدي|مواعيدي|معادي|معاداتي|حجزي|وصفتي|روشتتي|أدويتي|ادويتي|دوائي"
    "|نتائجي|تقريري|طبيبي|دكتوري|ملفي|حسابي|مرضاي|مرضايا"
    "|احجز|أحجز|الغاء|إلغاء|تأجيل|كلمة السر|كلمة المرور"
    "|الدكاترة المتاحين|الأطباء المتاحين|الاطباء المتاحين|مين الدكاترة"
    "|مين الأطباء|مين الاطباء|حجز معايا|حجزت معايا|حاجز معايا|حاجزة معايا"
    "|عندي معاد|عندي موعد|booked with me"
    "|المرضى اللي عندي|مرضى عندي"
    ")")

#: General medical-knowledge questions. Deliberately broad: the cost of a false
#: positive is a "not in the approved sources" reply to a question that was not
#: medical, while the cost of a false negative is an ungrounded medical answer.
_MEDICAL = re.compile(
    _WORD + "(?:"
    r"what (?:is|are|causes?|does)|what'?s"
    r"|how (?:do(?:es)?|can|is|are|long|often)"
    r"|why (?:do(?:es)?|is|are|am)"
    r"|(?:is|are) (?:it |there |they )?(?:safe|dangerous|normal|serious|"
    r"contagious|hereditary)"
    r"|difference between|tell me about|explain"
    r"|symptom|sign of|cause of|causes of|treat|treatment|therapy|cure"
    r"|diagnos|prevent|risk factor|complication|side ?effect|adverse"
    r"|normal range|blood pressure|blood sugar|cholesterol|infection|disease"
    r"|condition|syndrome|disorder|vaccin|screening|prognosis"
    # Arabic: what is / what are, how, why, symptoms, treatment, causes,
    # prevention, risks, complications, side effects, disease, is it...?
    "|ما هو|ما هي|ماهو|ماهي|ما الفرق|كيف|لماذا|ليه|إيه هو|ايه هو"
    "|أعراض|اعراض|علاج|سبب|أسباب|اسباب|الوقاية|مخاطر|مضاعفات"
    "|أعراض جانبية|اثار جانبية|آثار جانبية|مرض|التهاب|تشخيص|لقاح|تطعيم"
    ")")


def mode():
    """The configured grounding mode: ``on``, ``off`` or ``auto``."""
    value = (os.environ.get(GROUNDING_VAR) or "auto").strip().lower()
    return value if value in {"on", "off", "auto"} else "auto"


def looks_like_medical_question(message):
    """Is this a general medical-knowledge question?

    A heuristic, and only ever consulted to decide *how to fail*: if retrieval
    already found relevant sources, the answer is grounded whatever this says.
    Its job is to stop "what causes chest tightness?" being answered from
    training data when the corpus has nothing on it.
    """
    text = (message or "").strip().casefold()
    if not text:
        return False
    if _PERSONAL.search(text):
        return False
    return bool(_MEDICAL.search(text))


def _corpus_has_documents():
    """Does the knowledge base hold anything retrievable at all?"""
    from knowledge import retrieval

    try:
        return retrieval.retrievable_documents().exists()
    except Exception:                                       # noqa: BLE001
        # A database problem is not a reason to answer a medical question
        # ungrounded. Report "there is a corpus" so the medical path is taken
        # and the failure surfaces as a safe refusal rather than an improvised
        # answer.
        logger.exception("Could not read knowledge-base coverage")
        return True


def attempt(message, *, tools_available=False):
    """Try to answer ``message`` from the knowledge base.

    Returns a :class:`knowledge.rag.service.RAGAnswer` when the knowledge path
    owns this question — grounded or safely refused — and ``None`` when it does
    not, meaning the caller should use its own path.

    ``tools_available`` says whether the caller can instead look the answer up
    in Roshada. It decides what happens to a message that does not *look* like
    a medical question:

    * **With tools** — hand it over. A question the corpus was never going to
      answer ("is Layla booked with me?") must not be turned into "I couldn't
      find that in the approved sources" when the platform knows the answer.
    * **Without tools** — retrieve anyway. Nothing else can answer it, so the
      corpus is worth a try, and a hit still grounds a question the heuristic
      did not recognise.

    A message that *does* look like general medical knowledge is grounded
    either way. That is the invariant tools do not get to weaken.

    Imported inside the function on purpose: :mod:`knowledge.rag.service`
    imports this package's ``llm``, ``prompts`` and ``validation`` modules, and
    a module-level import here would close that loop at startup. The dependency
    direction that matters is unchanged — the assistant knows about RAG, RAG
    knows about the LLM facade, and the facade knows about Groq.
    """
    from knowledge.rag import service as rag

    setting = mode()
    if setting == "off":
        return None

    message = (message or "").strip()
    if not message:
        return None

    medical = looks_like_medical_question(message)
    if not medical and (tools_available
                        or _PERSONAL.search(message.casefold())):
        # Roshada's own data, not the medical literature.
        return None

    result = rag.answer(message)

    if not result.degraded:
        return result

    if result.reason == "fabricated_citation":
        # Degraded, but still a grounded answer with a warning attached. The
        # warning is the point — discarding it here would hide it.
        return result

    if result.reason == "no_context":
        if setting == "auto" and not _corpus_has_documents():
            # Not a retrieval failure: this deployment has no corpus yet.
            # Answering every medical question with "I found nothing" would be
            # a worse and less honest outcome than the assistant's own reply,
            # which still carries the safety prompt and validation.
            logger.info("Grounding skipped: the knowledge base is empty")
            return None
        return result if medical else None

    # unavailable | failed | rejected | invalid_query.
    if medical:
        # Fail safe. Handing the question to the provider now would bypass
        # grounding at the exact moment grounding is broken.
        logger.warning("Grounded answer unavailable (%s); not falling back to "
                       "an ungrounded medical answer", result.reason)
        return result
    return None
