# Interview checklist: draft topic definitions (for review)

**Status:** draft 3, 8 October 2026. The owner has answered Q1-Q14 and approved the follow-up split. These definitions are now in the tool as `packs/cps_interview_v2.yaml`, pending John Smith's review.
**Reviewers:** John Smith (CPS subject-matter expert) and the project owner.

## Why this matters

The tool marks each checklist topic while the interview happens. Every model we tested reads these definitions word for word, so loose definitions cause most disagreements. The examples below are made up and deliberately **not** taken from the test interview.

## Two kinds of follow-up (approved by the owner, 8 October 2026)

Owner's observation: *sometimes follow-up is absolutely necessary; often it is only a suggestion to get more information.* Today the tool has a single status, "needs follow-up", for both. Proposal:

- Each topic keeps a **coverage status**: not covered, partial, covered.
- Separately, a topic can carry a **follow-up marker**:
  - **Follow-up required**: the worker should not leave this unaddressed (e.g. the child says they feel unsafe without saying why).
  - **Follow-up suggested**: worth one more question, but don't force it (e.g. the child says they don't remember).
- When the worker taps **Asked** on the follow-up card, a *suggested* marker clears; the coverage status stays as it is. A *required* marker clears only when the answer arrives or the worker dismisses it.

This replaced the tool's single "needs follow-up" status.

## Rules that apply to every topic

1. **Only the parent's or child's own words count**, except where a topic says otherwise. The worker's questions, or the worker reading out the report, never cover a topic on their own.
2. **Covered:** every required part has been said. **Partial:** at least one required part, not all. **Not covered:** nothing relevant yet.
3. **Follow-up required** when statements contradict each other (owner, Q11), or the child mentions feeling afraid or unsafe without saying why or when, or answers "I don't know" to a safety question.
4. **Follow-up suggested** when an answer is vague, avoids the question, or a useful detail is missing.
5. The tool never judges whether anyone is telling the truth, and never says whether abuse happened. Statuses describe only **what has been said**.

## The 11 topics

### 1. Consent to recording
- **Required:** the parent agrees and the child agrees.
- **Partial:** one of them has agreed. **Covered:** both have.
- **If either refuses: the tool stops** (owner, Q1), **after the worker confirms** a "Consent refused: stop recording?" prompt (owner, Q12). The stop is written to the audit log.
- *Agreed timeline for the test interview:* partial when the parent first agrees to the tool, covered once the child consents.

### 2. Child's age and school grade
- **Required:** age and current grade (or not in school).
- **Who may answer** (owner, Q2): for a child **below 2nd grade**, the parent's answer counts. For a child in **2nd grade or above**, the child must say it; if only the parent says it, mark **partial**.
- **Follow-up required:** the parent and child give different answers (contradiction, Q11).
- *Example, partial:* Parent of a 4th grader: "She's nine, in fourth grade." (Child hasn't said it.)

### 3. Parent's account of how the child was hurt
- **Required, in the parent's words:** what happened and where.
- **When it happened** is not required (owner, Q13), but if it is missing, **follow-up is suggested**. Research: CPS guidance uses the time of injury to check whether an account fits the injury's age, to spot a delay in seeking care, and to establish who was supervising.
- **Useful, not required** (owner, Q3): whether the parent saw it or was told, and by whom. If the parent wasn't home, there is nothing more to get.
- **Useful, not required; follow-up suggested if missing** (owner, 8 October 2026, from the CPS sources):
  - **when the parent first noticed the injury**, separate from when it happened (the gap between noticing it and seeking care is something investigators look at);
  - **who was supervising the child when it happened** (links this account to topic 6).
- **Partial:** only some of what / where.
- **Follow-up required:** the account changes during the interview, or contradicts the child's.

### 4. Child's account of how they were hurt
- **Required, in the child's words:** what happened, where, who was there. **When** is not required, but if missing, follow-up is suggested (as topic 3).
- **"I don't know / I don't remember"** (owner, Q4): mark **partial** with **follow-up suggested**: the worker may probe once, but should not force an answer. If the child still can't remember after being asked, it stays **partial**.
- **Follow-up required:** the account contradicts the parent's.
- *Example:* Child: "I don't remember. It just happened." → partial, follow-up suggested.

### 5. Who lives in the household
- **Required:** everyone who lives in the home, named, with their relationship to the child.
- **The parent and the child in the interview count as named** by taking part (owner, 8 October 2026).
- **No "that's everyone" confirmation needed** (owner, Q5), but **when covered, follow-up is suggested**: ask whether anyone else lives or stays there (owner, 8 October 2026).
- **Partial:** some people named, or relationships missing.
- **Later:** a household list supplied before the visit (case context) can be cross-checked against what is said.

### 6. Who cares for the child while the parent works
- **Required:** who (name) and their relationship to the child.
- **Not required** (owner, Q6): how often or when ("most days", "sometimes"), or whether anyone else watches the child.
- **If the report names a person, the answer must say whether that person looks after the child** (owner, Q6 and Q14). The tool tracks whether this was asked and answered; it never judges whether anything happened.
- **Partial:** a caregiver mentioned without a name or relationship ("a neighbour helps").
- *Example, covered:* Parent: "My mom, his grandma, watches him after school."

### 7. Child's feelings of safety at home
- **Required, in the child's words:** whether they feel safe at home, and why or when not.
- **Partial:** "I feel safe" with no reason.
- **Follow-up required** (owner, Q7): the child says they feel unsafe, afraid, or that someone scares them, without saying why or when. The worker should probe further.
- **Once the child gives even a short reason or time** (for example "When he yells."), follow-up becomes **suggested**, not required: the caseworker decides whether to probe further (owner, 8 October 2026).
- **Too vague to count as a reason:** describing the person ("he is sometimes mean") rather than why or when the child feels unsafe. Follow-up stays **required** (owner, 8 October 2026).
- *Example, follow-up required:* Child: "Sometimes I don't like being home."
- *Example, covered:* Child: "I feel safe. My mom and my brother are nice to me."

### 8. Medical attention for the injury
- **Required:** whether the child was seen by a doctor, nurse or clinic; if yes, where and when.
- **Covered:** "no medical attention was sought" is a complete answer.
- **If no care was sought, the reason given** (for example "it didn't seem that bad"): **follow-up suggested if no reason was given** (owner, 8 October 2026). The tool records only whether a reason was given, never whether it is a good one.
- **Partial:** "yes" without where or when.
- **Follow-up required:** the parent and child contradict each other (Q11).

### 9. Prior injuries, earlier concerns and other injuries now
- **Required:** the worker asked about (a) earlier injuries or concerns and (b) any other injuries or marks the child has now (owner, 8 October 2026, from the CPS sources: more than one injury at different stages of healing is a key factor), and **both the parent and the child answered** (owner, Q8: a parent's answer alone is not enough).
- **Partial:** only one of them has answered, or only (a) or (b) was asked.
- **Follow-up required:** the answers contradict each other (Q11). **Follow-up suggested:** an answer is vague.

### 10. Other people to contact
- **Required:** at least one person outside the home, specific enough to reach: a name plus role or place ("Ms Lopez, her teacher at Oak Elementary").
- **Partial:** a role without a name or place ("her teacher").
- **One contact is enough** (owner, Q9). **Follow-up suggested** when no school contact has been named: the worker may choose not to pursue it.

### 11. Safety plan and next steps explained to the family
- **Required:** the worker explains what happens next, and any safety plan; the family acknowledges.
- **If no safety plan is needed, explaining next steps is enough** (owner, Q10).
- **Partial:** explained, but no acknowledgement from the family.

## After the interview (review screen, milestone M5)

- **Accounts not yet heard** (owner, 8 October 2026): when the report names another adult (for example the person said to have been caring for the child) who was not in the interview, the review lists "account not yet heard from <name>" as an option for the worker. The CPS sources treat conflicting accounts between two adult caretakers as important; the tool only notes whose account is missing, never what it would show.
- **Incident timeline** (approved): what was said about *when*: when it happened, when it was noticed, who was supervising, when care was or wasn't sought. Each item attributed to a speaker, with its quote, no conclusions.
- **Side-by-side accounts** (approved): the parent's, the child's and the report's version of the same topic next to each other, with quotes. The tool never labels a version as true.

## During the interview (milestone M3)

- **"Before you leave" prompt** (approved): when the interview seems to be ending, show the required topics still open.
- **Age-appropriate follow-up wording** (approved): suggested questions are phrased for the child's age (from topic 2), open-ended and non-leading.

## Answered follow-up questions (owner, 8 October 2026)

- **Q11.** Parent and child accounts contradict → follow-up **required**.
- **Q12.** Consent refusal → prompt the worker and wait for confirmation.
- **Q13.** "When" dropped from required for topics 3 and 4; missing "when" raises a suggested follow-up.
- **Q14.** Yes: the caregiver answer must say whether the person named in the report looks after the child.

- **Short safety answers.** "When he yells." after "he just scares me": covered, with follow-up **suggested**, not required; the caseworker decides.

## Effect on the test interview's answer key

- **Caregivers:** partial → **covered**. "Dave was here, he's here most days", with Dave's relationship given earlier, now meets the definition.
- **Child's account:** stays **partial**, now with **follow-up suggested** ("I don't remember what happened").
- **Child's feelings of safety:** partial with **follow-up required** from "I am afraid of him" / "he just scares me"; **covered with follow-up suggested** after "When he yells."
- **Prior injuries:** stays **not covered** (no one asked).
- **Household:** **covered** from "Dave is my boyfriend and lives here" (mother and Jill count as named), with follow-up **suggested** to ask about anyone else.
- **Parent's account and medical attention:** unchanged. The parent says when she noticed it ("Tuesday night"), who was there ("Dave was here"), and why no care was sought ("it didn't seem that bad").

New topics under consideration for later are in `docs/topic-ideas-later.md`.
