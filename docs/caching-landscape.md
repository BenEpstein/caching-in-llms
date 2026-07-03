# LLM Caching Landscape — Syllabus Source Review

> Built from every technical link in `docs/references/שיבוץ להרצאות.docx` (the "Caching in LLMs" course syllabus). 4 open-source libraries + 37 research papers. Emails and `bgu.ac.il` ignored. A few papers were named in the syllabus without a link (TurboRAG, RAGCache, Mooncake, LeanKV) — their canonical sources were resolved and included.
>
> **Purpose:** a working map to help pick a baseline OSS cache and mine concrete, benchmarkable policy ideas to bolt onto it (per the project rubric: Correctness 40% + Reproducibility 30% > Performance 15% > Clarity 15%).

---

## How to read this

Every entry is tagged on three axes:

- **Type** — `OSS library` · `Research paper` · `Talk/position`
- **Status** — repos: `active` (commits ≤3mo) / `stale` (3–12mo) / `not-active` (>12mo) / `archived`; papers: `has-code` / `active-area` / `one-off`
- **Actionability for *this* project** — how the idea maps onto a request/response-level semantic cache (GPTCache/ModelCache style) that you extend with a new policy:
  - 🟢 **Drop-in policy** — implementable as a pluggable eviction / similarity / embedding extension, directly benchmarkable vs. LRU/LFU.
  - 🟡 **Adaptable concept** — the principle transfers but needs re-derivation or non-trivial engineering.
  - 🔴 **Out-of-layer** — operates on attention KV tensors / serving infra / is an analysis paper; informs design but is not a drop-in feature.

The single most useful column is **Actionability** — it's the bridge from "interesting paper" to "thing I can ship and measure."

---

## Master table (scan this first)

| # | Source | Type | Status | Layer | Action | The new thing in one line |
|---|--------|------|--------|-------|:------:|---------------------------|
| **OSS libraries — the extension targets** |
| L1 | **GPTCache** | OSS lib | stale (Jul'25) | request/response | 🟢 | Semantic similarity match + **pluggable eviction interface** (`EvictionBase`) |
| L2 | **LMCache** | OSS lib | **active** | KV-tensor | 🔴 | KV-tensor prefix offload for vLLM; **no eviction policy exists** |
| L3 | **ModelCache** | OSS lib | stale (Jun'25) | request/response | 🟡 | Multi-tenant, model-scoped semantic cache; only bulk eviction |
| L4 | **Caffeine** | OSS lib | **active** | general (Java) | 🟡 | **W-TinyLFU** admission+eviction; ships a policy *simulator* |
| **Request-level / semantic caching papers** |
| 1 | Cache me if you can | paper | one-off | request/response | 🟡 | Online distillation: cache trains a cheap student to skip the LLM |
| 2 | MeanCache | paper | active-area | request/response | 🟡 | Federated similarity model + **context-chain as a 2nd key** |
| 3 | **SCALM** | paper | one-off | request/response | 🟢 | **Token-savings-weighted** eviction priority |
| 4 | **Cortex** | paper | active-area | request/response | 🟢 | **LCFU eviction** = cost×freq×latency×staticity/size + Markov prefetch |
| 5 | GenCache | paper | has-code | request/response | 🟡 | Cache stores a **synthesized program**, not a fixed response |
| 6 | SCOLAP (OLAP) | paper | one-off | request/response | 🟡 | **Canonicalize-then-exact-match** key (LLM intent signature) |
| 7 | WebCache (affordable web) | paper | one-off | image/CDN | 🔴 | Multimodal LLM-as-judge for replaceable images (wrong domain) |
| 8 | **Ensemble Embedding** | paper | active-area | request/response | 🟢 | **Multi-model ensemble embedding** + learned meta-encoder |
| 9 | **ContextCache** | paper | has-code | request/response | 🟢 | **Context-aware 2-stage match** (self-attn over prior turns) |
| 10 | Auditing Prompt Caching | paper | active-area | analysis | 🔴 | Timing audit proves cross-user cache sharing leaks |
| **KV-cache compression / eviction papers** |
| 11 | **H2O** | paper | has-code | KV-tensor | 🟡 | **Heavy-hitter** (cumulative attention) + recency eviction |
| 12 | SnapKV | paper | has-code | KV-tensor | 🟡 | Query window predicts which prompt tokens matter |
| 13 | **Scissorhands** | paper | has-code | KV-tensor | 🟡 | **Persistence of importance** → decayed hit-counter eviction |
| 14 | **Ada-KV** | paper | has-code | KV-tensor | 🟡 | **Adaptive budget allocation across buckets** |
| 15 | KIVI | paper | has-code | KV-tensor | 🔴 | Asymmetric 2-bit KV quantization |
| 16 | LeanKV | paper | active-area | KV-tensor | 🟡 | Continuous quantize↔prune spectrum, per-bucket budgets |
| 17 | StreamingLLM | paper | has-code | KV-tensor | 🟡 | **Attention sinks** → pin structural "anchor" entries |
| 18 | **SentenceKV** | paper | has-code | KV→semantic | 🟢 | **Semantic-unit grouping + similarity-gated hot tier** |
| 19 | KV-RAPTOR | paper | active-area | KV/RAG | 🟡 | Hierarchical **tree eviction** (leaves first, roots last) |
| **Serving systems / KV reuse / scheduling** |
| 20 | vLLM / PagedAttention | paper | **prod OSS** | KV-tensor | 🟡 | OS-style paging + copy-on-write **prefix sharing** |
| 21 | **SGLang / RadixAttention** | paper | **prod OSS** | KV-tensor | 🟢 | **Radix-tree (trie) prefix index** + LRU on nodes |
| 22 | Preble | paper | active-area | scheduling | 🟡 | Cache-locality-aware request routing |
| 23 | Sarathi-Serve | paper | active-area | scheduling | 🔴 | Chunked-prefill stall-free scheduling |
| 24 | InfiniGen | paper | has-code | tiered KV | 🟡 | **Importance-guided selective prefetch** from CPU |
| 25 | **FlexGen** | paper | has-code | tiered | 🟡 | **LP-optimized** recompute-vs-store-vs-offload |
| 26 | ServerlessLLM | paper | has-code | tiered | 🟡 | Checkpoint locality-aware scheduling + multi-tier staging |
| 27 | **Mooncake** | paper | **prod** | tiered+sched | 🟢 | **SLO-aware admission control** + cluster-wide tiered KV pool |
| 28 | vAttention | paper | active-area | KV-tensor | 🔴 | CUDA-VMM contiguous virtual KV (kernel detail) |
| 29 | **CachedAttention** | paper | active-area | tiered | 🟢 | **Persistent RAM→DRAM→SSD KV across turns** + async prefetch |
| 30 | KVPR | paper | has-code | tiered | 🟡 | Store small activations, **recompute** KV (I/O-aware) |
| **RAG caching + Knowledge Delivery Network** |
| 31 | CacheBlend | paper | has-code (in LMCache) | RAG/KV | 🔴 | Non-prefix KV reuse via selective partial recompute |
| 32 | TurboRAG | paper | has-code | RAG/KV | 🟡 | Offline-precompute chunk KV → warm-cache idea |
| 33 | **RAGCache** | paper | one-off | RAG/KV | 🟢 | **Cost-aware eviction = recompute-cost × retrieval-freq** |
| 34 | **KDN / LMCache** | position+talk | has-code | serving infra | 🟡 | KV cache as a transferable **network object** (CDN-for-LLMs) |
| **Cache security / side-channels** |
| 35 | **Early Bird** | paper | active-area | security | 🟢 | Cache-hit **timing leaks prompts** token-by-token |
| 36 | InputSnatch | paper | active-area | security | 🟡 | Statistical timing attack survives jitter |
| 37 | **Key Collision** | paper | active-area (Jan'26) | security | 🟢 | **Locality ⊥ collision-resistance**: semantic caches are hijackable |

---

## 1 · Open-source libraries (your baseline candidates)

These four are what the syllabus's "open source tutorial" lecture covers, and they *are* the universe of things you'd fork for §2. Only one is a clean fit for "add a new eviction policy to a request-level semantic cache."

### L1 · GPTCache — semantic request/response cache with pluggable eviction
- **Link:** https://github.com/zilliztech/GPTCache · 8.0k★ · last commit **Jul 2025** · not archived
- **Status:** **stale** (no commits in ~10mo; last release v0.1.44 Aug 2024) — maintenance risk, but stable and forkable.
- **Main flow:** query → embedding model (OpenAI/ONNX/local) → vector store ANN search (Milvus/FAISS/Chroma) → `SimilarityEvaluator` compares top hit to a configurable threshold → hit returns the stored full response; miss calls the LLM and writes `(embedding, response)` to vector + scalar stores → eviction fires at `max_size` via `MemoryCacheEviction.popitem()` (delegates to `cachetools`) with an `on_evict` callback to purge persistent storage.
- **The new thing:** semantic similarity matching — paraphrases hit the same entry, gated by a cosine threshold.
- **Extension seam (why this is the baseline):** `gptcache/manager/eviction/base.py` defines abstract `EvictionBase` (`put`/`get`/`policy`); `MemoryCacheEviction` already supports `LRU/LFU/FIFO/RR` via a policy string. **Add a policy = subclass `EvictionBase` + register in the factory.** `SimilarityEvaluator` and `embedding_func` are independently swappable. This is exactly the pluggable seam the rubric needs.
- **Baseline fit: 4/5.** Best fit by far; only knock is staleness (which actually *helps* the §4 "merged upstream PR" 100-grade path — a quiet repo may welcome a clean eviction-policy PR).

### L2 · LMCache — KV-tensor prefix offload for vLLM
- **Link:** https://github.com/LMCache/LMCache · 8.3k★ · last commit **2026-05-23** · not archived
- **Status:** **active** (nightly releases). This is the reference implementation of the KDN idea (#34).
- **Main flow:** vLLM request → tokens chunked → rolling SHA-256 prefix hash per chunk → `storage_backend.batched_get(keys)` → hit injects raw KV blobs into attention, skipping prefill → miss runs inference, serializes KV back via `batched_put()` → backends: local RAM dict / local disk safetensors / remote.
- **The new thing:** KV-tensor prefix offload — reuses attention KV for any shared prompt prefix, near-zero prefill for those tokens.
- **Extension seam:** poor for *this* project. `LMCBackendInterface` extends storage, but **there is no eviction interface — the cache grows unbounded** (an explicit TODO). "Adding eviction" here means inventing the interface, which is a bigger, harder-to-benchmark lift than extending GPTCache's existing one.
- **Baseline fit: 2/5.** Wrong layer (tensors, needs vLLM + GPU), no policy to extend. *But* it's the most "alive" repo and the §4 upstream-PR target with the most momentum if you want the KV-cache track instead.

### L3 · ModelCache — multi-tenant, model-scoped semantic cache
- **Link:** https://github.com/codefuse-ai/ModelCache · 947★ · last commit **Jun 2025** · not archived
- **Status:** **stale** (~11mo).
- **Main flow:** `Adapter` extracts query (multi-turn, scoped by model+tenant) → `Embedding` → vector store (Milvus/Redis/OceanBase) → `Rank` applies similarity threshold (separate long/short thresholds) → hit returns stored answer; miss stored via `data_manager`. Eviction is coarse: `truncate_by_model` clears a whole model/tenant scope — **no per-entry LRU/LFU/TTL**.
- **The new thing:** multi-tenant namespacing — `gpt-4` hits don't bleed into `gpt-3.5`.
- **Extension seam:** moderate. Four modules are config-swappable, but there's **no per-entry eviction abstraction** to implement against — you'd build it in `data_manager` from scratch.
- **Baseline fit: 2/5.** Conceptually right layer, but lower adoption and missing the seam GPTCache hands you.

### L4 · Caffeine — W-TinyLFU in-memory Java cache (+ simulator)
- **Link:** https://github.com/ben-manes/caffeine · 17.7k★ · last commit **2026-05-25** · not archived
- **Status:** **active** (v3.2.4 this month).
- **Main flow:** `get(key, loader)` → lock-free hash lookup → miss runs loader → at `maximumSize/Weight` the **W-TinyLFU** admission policy compares a Count-Min frequency sketch of the candidate vs. a victim → optional `removalListener` + async time-based expiry.
- **The new thing:** **W-TinyLFU** — frequency-sketch admission + segmented LRU, beating plain LRU at equal size. Ships a **policy simulator** the syllabus explicitly flags for "deduce which policy works best for you."
- **Extension seam:** `Policy<K,V>` is introspection-only; the algorithm is baked in (configure, don't swap). Java, not LLM-specific.
- **Baseline fit: 1/5 as a baseline**, **5/5 as a tool**: use its **simulator** to pre-screen candidate eviction policies on your trace before implementing them in GPTCache, and cite W-TinyLFU as the strong non-LLM baseline your policy must beat.

> **Baseline recommendation:** extend **GPTCache**. It's the only candidate with (a) the right cache layer, (b) an existing pluggable `EvictionBase`/`SimilarityEvaluator` seam, and (c) a quiet-but-stable repo that keeps the upstream-PR carrot open. Use **Caffeine's simulator** as your policy sandbox and W-TinyLFU as a baseline to beat.

---

## 2 · Request-level / semantic caching papers

This is the home turf — everything here matches GPTCache's layer, so portability is high.

### 3 · SCALM — Semantic Caching for Automated Chat Services 🟢
- https://arxiv.org/abs/2406.00025 · IEEE ICDCS 2024 · no code
- **Main flow:** cluster historical queries (DBSCAN → hierarchical CO-HSC/SE-HSC) into semantic patterns; rank each pattern by **token-saving ratio**; admit low-rank patterns when cold, mid/high-rank when full; evict lowest `score = pattern_rank + hit_freq` (FIFO tiebreak). Cosine on `text-embedding-3-small`, threshold 0.90.
- **The new thing:** value entries by **tokens saved per hit**, not recency/frequency — protect high-payoff patterns.
- **Portability:** **Drop-in eviction policy.** Score = `est_response_tokens × log(hit_count+1)`, all from insertion-time metadata. One-time offline clustering assigns ranks. Maps cleanly onto a custom `EvictionBase`.
- **Actionability: High** — clean policy, directly comparable to LRU/LFU on hit rate **and** token savings.

### 4 · Cortex — Semantic-Aware Knowledge Caching 🟢
- https://arxiv.org/abs/2509.17360 · USENIX NSDI 2026 · no code
- **Main flow:** each entry is a *Semantic Element* (embedding + value + latency + cost + **staticity** 1–10 + frequency). Two-stage *Seri* retrieval: ANN (τ≈0.9) → lightweight co-located LLM judge (τ_lsm). Eviction by **LCFU** = `log(f+1)·log(cost+1)·log(lat+1)·log(static+1) / size`. First-order Markov **prefetcher** pre-fetches the predicted next query.
- **The new thing:** **LCFU** composite eviction — evict entries that are cheap to re-fetch and rarely accessed, weighted by freshness.
- **Portability:** **Drop-in.** LCFU is pure per-entry metadata arithmetic. Staticity ≈ heuristic (factual vs. time-sensitive, or response length). LLM judge optional (replace with stricter threshold). Markov prefetcher is a separable bonus module.
- **Actionability: High** — even a simplified `cost × freq / size` beats LRU and is trivially benchmarkable.

### 9 · ContextCache — Context-Aware Semantic Cache for Multi-Turn 🟢
- https://arxiv.org/abs/2506.22791 · PVLDB 18(12) 2025 · **code:** github.com/uYanJX/ContextCache
- **Main flow:** stage 1 ANN over current-query embedding; stage 2 a **hierarchical self-attention** over current + prior-turn embeddings yields a context-aware score; serve only above threshold — kills false hits where the same string means different things in different conversations.
- **The new thing:** similarity decision incorporates **cross-turn dependencies**, not just the current query.
- **Portability:** **Drop-in similarity-evaluation extension** — inject a context scorer over the last *K* turn embeddings into GPTCache's `similarity_evaluation`; storage/eviction untouched. Reference code exists. Reports +10.9% precision / +14.8% recall over GPTCache.
- **Actionability: High** — published, has code, exact plug-point, benchmarkable on any multi-turn dataset.

### 8 · Ensemble Embedding 🟢
- https://arxiv.org/abs/2507.07061 · arXiv Jul 2025 · no code
- **Main flow:** N embedding models embed the query → a trained **meta-encoder** fuses them into one vector for ANN lookup. Trained on Quora Question Pairs; 92% hit on equivalents, 85% rejection of non-equivalents.
- **The new thing:** **ensemble + learned meta-encoder** as the similarity representation — different encoders catch different semantic facets.
- **Portability:** cleanest possible **drop-in for `embedding_func`**: call N encoders, fuse via a pre-trained (frozen) meta-encoder, return the vector. Touches only the embedding layer.
- **Actionability: High** — modular, pre-trainable on public paraphrase data, benchmarkable on hit precision/recall.

### 2 · MeanCache — User-Centric Semantic Caching 🟡
- https://arxiv.org/abs/2403.02694 · IEEE IPDPS 2025 · no code
- **Main flow:** per-user local cache; small embedding model; **federated** fine-tuning of the similarity model across devices (no raw queries centralized); context chains encoded as a secondary key.
- **The new thing:** federated similarity-model training + **context-chain as a 2nd matching key**.
- **Portability:** the **context-chain key** is portable — a custom `pre_embedding_func(query, history)` that fuses recent turns before indexing. Federated part needs multiple clients → out of scope.
- **Actionability: Med** — context-aware key is concrete; overlaps with ContextCache (which has code).

### 6 · SCOLAP — Semantic Caching for OLAP via LLM Query Canonicalization 🟡
- https://arxiv.org/abs/2602.19811 · DOLAP 2026 · no code
- **Main flow:** LLM rewrites each query into a structured **intent signature** (measures/dims/filters/time); cache keyed on the signature (exact-match on canonical form, not embedding ANN). Roll-up / filter-down derivations extend hits losslessly; confidence-gate rejects low-confidence NL parses.
- **The new thing:** **canonicalize-then-exact-match** — replace fuzzy embedding similarity with structured equivalence.
- **Portability:** the canonicalize-as-key idea is a portable `pre_embedding_func` (LLM normalizes the prompt → exact/near-exact lookup). The OLAP roll-up/filter-down derivations don't generalize.
- **Actionability: Med** — differentiated from embedding ANN; needs a paraphrase-labeled eval set; adds an LLM call on the write path.

### 5 · GenCache — Generative Caching for Structurally Similar Prompts 🟡
- https://arxiv.org/abs/2511.17565 (syllabus: openreview MHGViOjZ27) · NeurIPS 2025 · **code:** github.com/sarthak-chakraborty/GenCache
- **Main flow:** cluster prompts by dual thresholds (prompt 0.8 / response 0.75); when a cluster hits ≥4 exemplars, a CodeGenLLM synthesizes a **Python template program** (regex-extract variables → fill), validated by a ValidLLM (≥50% pass, ≤30 retries); future hits run the program locally instead of calling the LLM.
- **The new thing:** cache stores a **generated program** that parameterizes the response, so structurally-similar-but-varied prompts get correct, customized answers.
- **Portability:** architecturally invasive — needs LLM-in-the-loop at cluster maturation. The dual-threshold **clustering** alone is an extractable similarity strategy; full template synthesis changes the response path.
- **Actionability: Med** — code exists; clustering is easy, synthesis is heavy.

### 1 · Cache me if you can — cost-aware teacher-student 🟡
- https://arxiv.org/abs/2310.13395 · Findings of EMNLP 2023 · no code
- **Main flow:** cached responses train a cheap **student** (kNN/MLP); a cost-aware trust threshold decides student-serve vs. LLM-call; student retrains online as the cache grows. Evaluated only on intent/sentiment classification.
- **The new thing:** **online distillation from the cache** — the cache shrinks the call rate over time, not just on exact hits.
- **Portability:** a pre-lookup "should I even query the LLM?" classifier trained on cache contents, with a background retrain trigger. Domain-specific (needs labeled task data).
- **Actionability: Low** — heavy, task-specific, no clean eviction analogue.

### 7 · WebCache — LLM-Enabled Semantic Caching for Affordable Web Access 🔴
- https://neurips.cc/virtual/2025/loc/san-diego/133736 · WiML @ NeurIPS 2025 · no code
- **Main flow:** multimodal LLM judges whether a page **image** is semantically replaceable by a cached one (NRMSE scored), swaps it before sending to bandwidth-limited clients. 37% of images replaceable.
- **The new thing:** multimodal LLM-as-judge for content equivalence (images).
- **Portability:** **none** — image/CDN domain, not text prompts. The generic "LLM-as-judge" appears in Cortex in a usable form.
- **Actionability: Low.**

### 10 · Auditing Prompt Caching in Language Model APIs 🔴
- https://arxiv.org/abs/2502.07776 (syllabus: openreview gUj2fxQcLZ) · ICML 2025 · no code
- **Main flow:** not a cache — an **audit**. Send hit-inducing vs. miss-inducing request pairs, measure latency distributions, hypothesis-test for shared caching. Confirms **global cross-user cache sharing** in 7 providers incl. OpenAI; leaks OpenAI embedding architecture as a side effect.
- **The new thing:** timing audit proving shared caches leak.
- **Portability:** not a policy. Implication: **per-user cache isolation** (user_id in the key) if you go multi-tenant. Pairs with the security cluster below.
- **Actionability: Low** as a feature; **High** as motivation for a security-aware extension.

---

## 3 · KV-cache compression / eviction papers

These operate on attention KV tensors (🔴 layer-wise), but several encode **eviction principles** that transfer to ranking *whole cached entries* (🟡). Mine the principle, not the mechanism.

### 11 · H2O — Heavy-Hitter Oracle 🟡
- https://arxiv.org/abs/2306.14048 · NeurIPS 2023 · **code:** github.com/FMInference/H2O
- **Main flow:** track **cumulative attention** per token; keep a budget of {recent tokens} ∪ {highest-cumulative-attention "heavy hitters"}; evict the rest each step (submodular framing).
- **The new thing:** cumulative attention as a frequency proxy → heavy-hitter retention.
- **Transfer:** strong — request-level analogue is `α·hit_count + β·recency`, an LFU+LRU hybrid with theoretical backing for why frequency-weighting beats pure recency.
- **Actionability: High** (as the theoretical spine for a frequency+recency policy).

### 13 · Scissorhands — Persistence of Importance 🟡
- https://arxiv.org/abs/2305.17118 · NeurIPS 2023 · **code:** github.com/lzcemma/Scissorhands
- **Main flow:** "importance is persistent" — a token attended-to now stays important later; probabilistically retain high-historical-attention pivots within a fixed budget.
- **The new thing:** past importance predicts future importance → historical scores suffice (no per-step re-eval).
- **Transfer:** justifies an **exponentially-decayed hit counter** as the eviction key (vs. raw recency). Concrete policy design choice.
- **Actionability: High** (clear implementation path, theoretical backing).

### 14 · Ada-KV — Adaptive Budget Allocation 🟡
- https://arxiv.org/abs/2407.11550 · NeurIPS 2025 · **code:** github.com/FFY0/AdaKV
- **Main flow:** don't split the eviction budget uniformly across attention heads; derive per-head budgets from each head's attention concentration (sparse heads get less, diffuse heads get more), bounding total error.
- **The new thing:** **per-bucket adaptive budget allocation** under a fixed global budget.
- **Transfer:** segment the cache into topic/semantic clusters, measure per-cluster hit-rate variance, **allocate eviction budget per cluster** instead of globally. Novel differentiator over flat LRU/LFU.
- **Actionability: High** (a genuinely new policy idea, not just a re-skin of LFU).

### 18 · SentenceKV — Sentence-Level Semantic KV Caching 🟢
- https://arxiv.org/abs/2504.00970 · COLM 2025 · **code:** github.com/zzbright1998/SentenceKV
- **Main flow:** group tokens into sentences, store a compact **semantic vector** per sentence on GPU, offload token KV to CPU; at decode, cosine-match the query to sentence vectors and pull only top-k sentences' KV back. Two-tier GPU/CPU hierarchy gated by semantic similarity.
- **The new thing:** **semantic-unit grouping + similarity-gated hot tier** — the closest KV paper to request-level semantic caching.
- **Transfer:** the embed→retrieve-by-similarity→serve-from-hot-tier pipeline *is* GPTCache. Suggests clustering entries by topic and using cluster-level retrieval scores to drive admission/eviction.
- **Actionability: High** — a ready blueprint for a semantic-cluster eviction policy.

### 17 · StreamingLLM — Attention Sinks 🟡
- https://arxiv.org/abs/2309.17453 · ICLR 2024 · **code:** github.com/mit-han-lab/streaming-llm
- **Main flow:** first tokens get huge attention regardless of content ("sinks"); keep {few initial tokens} + {sliding window} for infinite streaming.
- **The new thing:** some entries must be **pinned** by structural role, not content/recency.
- **Transfer:** **anchor-pinning** — always retain a small set of structurally critical entries (e.g., a system prompt or hot document prefix) regardless of LRU pressure. Detection heuristic must be redesigned for request-level.
- **Actionability: Med.**

### 12 · SnapKV 🟡
- https://arxiv.org/abs/2404.14469 · NeurIPS 2024 · **code:** github.com/FasterDecoding/SnapKV
- **Main flow:** the query window reveals which prompt positions each head will attend to; snap the cache to that selection before decoding.
- **The new thing:** **query-predictive** prefill compression.
- **Transfer:** "the incoming query reveals which cached content matters" → retrieval-side relevance scoring, not eviction-side. **Med.**

### 19 · KV-RAPTOR — Tree-Structured Retrieval + KV Compression 🟡
- https://www.semanticscholar.org/paper/KV-RAPTOR... · SBBD 2025 · no code
- **Main flow:** RAPTOR-style recursive **summary tree** (leaves = passages, parents = abstractions); top-down traversal with KV compression so coarse retrieval is cheap, precise retrieval pays full cost.
- **The new thing:** **hierarchical tree eviction** — evict leaves first, keep roots (general/frequently-relevant) longest.
- **Transfer:** multi-granularity cache: store both detailed pairs (leaves) and cluster summaries (parents); prune leaves under pressure. Novel structure vs. flat LRU.
- **Actionability: Med** (requires building the tree).

### 16 · LeanKV 🟡
- https://arxiv.org/abs/2412.03131 · arXiv Dec 2024 · no public repo
- **Main flow:** treat quantization↔pruning as a continuum; Hetero-KV (keys > values bit-width), per-head dynamic sparsity, three-tier importance (full→compressed→pruned).
- **The new thing:** **continuous importance→storage-tier** mapping with per-bucket dynamic budgets.
- **Transfer:** tiered cache (hot full / warm compressed / cold evicted) with dynamically-sized tier boundaries. No new *ranking* signal (borrows H2O/Ada-KV). **Med.**

### 15 · KIVI — Asymmetric 2-bit KV Quantization 🔴
- https://arxiv.org/abs/2402.02750 · ICML 2024 · **code:** github.com/jy-yuan/KIVI
- **Main flow:** per-channel quant for keys, per-token for values (different statistical geometry), 2-bit + small full-precision residual.
- **The new thing:** asymmetric precision matched to key vs. value statistics.
- **Transfer:** **low** — about compression fidelity, not which entries to keep. Only relevant if you add a compressed storage tier.
- **Actionability: Low.**

---

## 4 · Serving systems / KV reuse / scheduling

Big infra systems. Mostly 🔴/🟡 for a request-level cache, but the **prefix-trie**, **tiered RAM/disk**, **cost-aware recompute-vs-store**, and **SLO-aware admission** ideas are gold.

### 21 · SGLang / RadixAttention 🟢
- https://arxiv.org/abs/2312.07104 · MLSys 2025 · **code:** github.com/sgl-project/sglang (production OSS)
- **Main flow:** a **radix tree (trie)** indexes all live KV blocks by token sequence → any prior prefix is auto-reused via longest-prefix match → LRU eviction on trie nodes.
- **The new thing:** trie-indexed prefix reuse with O(prefix) lookup.
- **Transfer:** **most directly transferable structure here.** Index cached `(prompt, response)` pairs in a trie over word/token n-grams → lookup = longest-prefix match (enables **partial hits**) → cost-weighted LRU on nodes (nodes reachable by many prefixes cost more to evict). Gives you a concrete data structure, not a vibe.
- **Actionability: High.**

### 27 · Mooncake — KVCache-centric disaggregation 🟢
- https://arxiv.org/abs/2407.00079 · production at Moonshot/Kimi · no public code
- **Main flow:** disaggregate prefill vs. decode clusters; cluster-wide **tiered KV pool** (GPU/DRAM/SSD); KVCache-centric scheduler does **prediction-based early rejection** under overload + locality routing.
- **The new thing:** shared tiered KV as a first-class resource + **SLO-aware admission control**.
- **Transfer:** (1) **admission control** — only cache responses whose recompute cost exceeds a threshold (don't cache cheap one-token answers); (2) tiered GPU→DRAM→SSD migration by recency = a hierarchical eviction policy. Both directly portable.
- **Actionability: High** (admission filtering is an underused, easy, measurable win).

### 29 · CachedAttention — multi-turn persistent KV 🟢
- https://arxiv.org/abs/2403.19708 · USENIX ATC 2024 · no code
- **Main flow:** persist KV **across conversation turns** in GPU→DRAM→SSD; **layer-wise async prefetch** hides I/O; scheduler-aware eviction by turn age; positional-encoding decoupled to avoid invalidation.
- **The new thing:** persistent **hierarchical RAM/disk KV** with async prefetch, at conversation granularity.
- **Transfer:** this *is* the "hierarchical RAM+disk cache" candidate extension. Evict by `turn_age × P(future access)`; speculatively prefetch semantically similar entries from disk when a request arrives.
- **Actionability: High** (template for the hierarchical-cache track).

### 25 · FlexGen — LP-optimized offloading 🟡
- https://arxiv.org/abs/2303.06865 · ICML 2023 (Oral) · **code:** github.com/FMInference/FlexGen
- **Main flow:** offload weights + KV to CPU/NVMe; solve tensor **placement + access schedule as a linear program** over GPU/CPU/disk under memory limits; 4-bit weights, quantized KV.
- **The new thing:** **LP formulation** of recompute-vs-store-vs-offload with measured I/O bandwidths.
- **Transfer:** the most rigorous cost model for a hierarchical cache: keep in RAM if `recompute_cost > ram_read_latency × P(hit)`, spill to disk if `disk_read_latency < recompute_cost`, else evict. Yields a **principled, tunable** eviction threshold with clean math for the report.
- **Actionability: High** (gives the report a defensible analytical core).

### 24 · InfiniGen — importance-guided KV prefetch 🟡
- https://arxiv.org/abs/2406.19707 · OSDI 2024 · **code:** github.com/snu-comparch/InfiniGen
- **Main flow:** speculate next-layer important tokens via "minimal rehearsal," prefetch only those from CPU.
- **Transfer:** score entries by predicted future-hit probability; lightweight similarity scoring as the "rehearsal" to pre-warm RAM tier from disk. **Med-High.**

### 20 · vLLM / PagedAttention 🟡
- https://arxiv.org/abs/2309.06180 · SOSP 2023 · **code:** github.com/vllm-project/vllm (industry standard)
- **Main flow:** OS-style paging of KV into non-contiguous blocks via a block table; copy-on-write **prefix sharing**.
- **Transfer:** block-granularity eviction; tag entries by longest shared prefix and evict blocks no longer on any live prefix. **Med-High.** (Mostly a citation anchor for "why prefix reuse matters.")

### 22 · Preble — cache-locality-aware scheduling 🟡
- https://arxiv.org/abs/2407.00023 · arXiv 2024 · no code
- **Main flow:** two-level scheduler routes each request to the GPU already holding its prefix's KV.
- **Transfer:** **prefix-frequency-weighted retention** — keep entries on the hot path of many incoming prefixes; relevant if cache is sharded/tiered. **Med.**

### 26 · ServerlessLLM 🟡
- https://arxiv.org/abs/2401.14351 · OSDI 2024 · **code:** github.com/ServerlessLLM/ServerlessLLM
- **Main flow:** fast cold-start via loading-optimized checkpoints, multi-tier SSD→RAM→GPU staging, locality-aware scheduling, live migration.
- **Transfer:** multi-tier **pre-staging / prefetch** from disk to RAM on access prediction; route lookups to the warmest tier. **Med.**

### 30 · KVPR — I/O-aware partial recomputation 🟡
- https://arxiv.org/abs/2411.17089 · ACL Findings 2025 · **code:** github.com/chaoyij/KVPR
- **Main flow:** store small **activations** on CPU, recompute KV on GPU (cheaper than transferring full KV under PCIe limits); 3-stage compute/transfer overlap.
- **Transfer:** cache a **compressed/summarized** representation + cheap expansion vs. storing full text; cost-aware eviction weighing storage vs. re-derive cost. **Med** (needs embedding infra).

### 23 · Sarathi-Serve — chunked prefill 🔴
- https://arxiv.org/abs/2403.02310 · OSDI 2024 · **code:** github.com/microsoft/sarathi-serve (merged into vLLM)
- **Main flow:** chunk prefill, interleave with decode → stall-free scheduling.
- **Transfer:** not a cache; the chunk cost model motivates **cost-aware eviction weighting** and partial-prefix hits. **Med/Low.**

### 28 · vAttention 🔴
- https://arxiv.org/abs/2405.04437 · ASPLOS 2025 · no code
- **Main flow:** CUDA virtual-memory APIs keep KV virtually contiguous (so FlashAttention runs unmodified) while physically paged.
- **Transfer:** **low** — GPU kernel detail. Faint lesson: memory-map a disk tier for contiguous logical layout.
- **Actionability: Low.**

---

## 5 · RAG caching + Knowledge Delivery Network

### 33 · RAGCache — RAG-aware cost replacement 🟢
- https://arxiv.org/abs/2404.12457 · arXiv Apr 2024 · no code
- **Main flow:** cache retrieved-document intermediate states in a **knowledge tree** across GPU↔host; replacement combines **recompute cost × retrieval frequency**, keeping expensive+hot docs in fast memory.
- **The new thing:** eviction priority = compute-cost-weighted frequency (LRU/LFU ignore cost heterogeneity).
- **Transfer:** **drop-in cost-weighted eviction** — score = `retrieval_freq × response_generation_cost` (token count or measured latency). Benchmark vs. LRU under heterogeneous response lengths.
- **Actionability: Med-High** (clean, portable, benchmarkable; converges with SCALM/Cortex/FlexGen).

### 34 · KDN / LMCache — "Do LLMs Need a CDN?" 🟡
- https://arxiv.org/abs/2409.13761 + APNet 2024 slides · **code:** github.com/LMCache/LMCache
- **Main flow:** treat KV caches as transferable, composable **network objects** routed to GPUs across a cluster — a "CDN for LLM knowledge." LMCache is the prototype.
- **The new thing:** KV cache as a network-addressable artifact; caching granularity = "knowledge tensor," not "response."
- **Transfer:** below the request layer, but **this is the framing behind LMCache** — relevant if you take the KV-cache track for the upstream-PR carrot.
- **Actionability: Med** (strategic context for baseline choice).

### 32 · TurboRAG — precomputed chunk KV 🟡
- https://arxiv.org/abs/2410.07590 · arXiv Oct 2024 · **code:** github.com/MooreThreads/TurboRAG
- **Main flow:** precompute & serialize chunk KV **offline**; at query time load+concatenate (modified masks/positions, fine-tuned model), skipping prefill. Up to 9.4× TTFT.
- **Transfer:** core needs model fine-tuning (out of scope). Borrowable: **warm-cache initialization** — pre-populate the cache with embeddings of expected high-frequency queries at startup.
- **Actionability: Low-Med.**

### 31 · CacheBlend — non-prefix KV reuse for RAG 🔴
- https://arxiv.org/abs/2405.16444 (ACM 10.1145/3689031.3696098) · EuroSys 2025 · **code:** in LMCache
- **Main flow:** reuse precomputed chunk KV even when not a contiguous prefix, **selectively recompute** a small token subset to restore cross-chunk attention; pipeline retrieval+recompute.
- **The new thing:** approximate non-prefix KV reuse via partial recompute.
- **Transfer:** KV-tensor mechanism (out of layer). Design lesson: "good-enough approximate reuse beats no reuse" → motivates approximate/semantic matching thresholds.
- **Actionability: Low** (great citation for why request-level caching sidesteps the cross-attention problem).

---

## 6 · Cache security / side-channels — the differentiated angle

This cluster is where a **novel, publishable** extension hides: today's semantic caches are provably attackable, and **no OSS library ships a defense**. A "security-aware caching policy" is defensible, benchmarkable, and rare.

### 37 · Key Collision — From Similarity to Vulnerability 🟢
- https://arxiv.org/abs/2601.23088 · arXiv Jan 2026 (newest source) · no code
- **Main flow:** model embedding keys as fuzzy hashes; prove the **locality that gives high hit rates is mathematically incompatible with collision resistance**; build *CacheAttack*, which crafts queries whose embeddings collide with a target key → cache returns the attacker's response to victims. 86% hit rate across embedding models.
- **The new thing:** the locality⊥collision-resistance impossibility is *fundamental* to all semantic caches, not an implementation bug.
- **Transfer (THE candidate extension):** a **collision-resistant matching policy** — (1) second-pass exact/crypto check after ANN similarity; (2) per-user embedding salting; (3) entropy/confidence-dynamic thresholds trading recall for robustness. Benchmark hit rate vs. adversarial-success rate as a Pareto curve.
- **Actionability: High** — newest, unmitigated, structural flaw; clean policy defense with crisp metrics.

### 35 · Early Bird — Timing Side Channels in LLM Serving 🟢
- https://arxiv.org/abs/2409.20002 · IEEE TIFS 2025 · no code
- **Main flow:** shared cache → response time depends on hit/miss → an adversary times queries and reconstructs other users' prompts / system prompts **token-by-token**. Demonstrated black-box on live services.
- **The new thing:** prefix-cache hit timing is a **prompt-extraction side channel**.
- **Transfer:** **timing-resistant policy** — (1) response-time jitter, (2) per-user namespace isolation, (3) probe-pattern rate limiting. Metric = bits leaked per query / attacker success rate, with vs. without defense.
- **Actionability: High.**

### 36 · InputSnatch 🟡
- https://arxiv.org/abs/2411.18191 · arXiv Nov 2024 · no code
- **Main flow:** same timing channel, recovers the user's own input; ML candidate generator + statistical outlier elimination survives real-world jitter.
- **The new thing:** **naive random jitter is insufficient** — a statistical attacker filters it out.
- **Transfer:** raises the bar — a defensible defense needs DP-style (Laplace) noise or constant-time buffered serving, *proven against an adaptive attacker*. Pairs with Early Bird.
- **Actionability: Med** (sharpens the threat model; not a separate design).

---

## 7 · Synthesis — new ideas to add to the OSS caches (ranked)

Everything below plugs into **GPTCache's** existing seams (`EvictionBase`, `SimilarityEvaluator`, `embedding_func`, `pre_embedding_func`) and is benchmarkable against its built-in LRU/LFU/FIFO — i.e., it satisfies Correctness + Reproducibility first, with a measurable gain.

### Where each idea plugs in
- **Eviction scoring (`EvictionBase`):** SCALM (token-savings), Cortex LCFU (cost·lat·static·freq/size), RAGCache (cost×freq), H2O/Scissorhands (decayed heavy-hitter), Ada-KV (per-cluster adaptive budget), FlexGen (LP recompute-vs-store), Mooncake (admission control), StreamingLLM (anchor pinning).
- **Similarity matching (`SimilarityEvaluator`):** ContextCache (context-aware), Cortex Seri (ANN+judge), Key Collision (collision-resistant 2nd check).
- **Embedding (`embedding_func`):** Ensemble Embedding (meta-encoder).
- **Key construction (`pre_embedding_func`):** MeanCache (context-chain), SCOLAP (canonicalization).
- **Architecture / tiering:** CachedAttention + FlexGen + Mooncake (hierarchical RAM/disk with cost-aware admission); SentenceKV / KV-RAPTOR (semantic-cluster / tree organization).
- **Security policy:** Key Collision + Early Bird (collision/timing-resistant matching & serving).

### Top recommendations for the project

1. **Composite cost-aware eviction policy** (🟢 highest rubric value). Fuse the three independent papers that all converge on the same insight — **value an entry by what it saves, not when it was used**: `score = f(hit_freq) · recompute_cost · token_savings / size`, with a freshness/staticity decay. SCALM gives token-savings, Cortex gives the LCFU shape, RAGCache gives cost×freq, FlexGen gives the analytical threshold, H2O/Scissorhands give the frequency-decay justification. Implement as one `EvictionBase` subclass with tunable weights (the rubric's "tunable parameters" requirement), benchmark vs. LRU/LFU/FIFO on a repetitive-prompt workload. **This is the safe, strong core extension.** Ablate each term for §5.

2. **Context-aware matching for multi-turn** (🟢 has reference code). Port ContextCache's two-stage scorer into `SimilarityEvaluator`. Proven +10.9% precision / +14.8% recall over GPTCache, with code to validate against. Cleanly benchmarkable; pairs naturally with #1 (better matching + better eviction = compounding gains, with an ablation showing each).

3. **Security-aware policy — the novelty/100-grade swing** (🟢 rare, publishable). No OSS semantic cache defends against the **2026 Key Collision attack** or the **Early Bird timing channel**. Add a collision-resistant matching mode (post-ANN exact/salted check + entropy-dynamic threshold) and/or timing-resistant serving, then report a hit-rate-vs-adversarial-success Pareto curve. This is the kind of "actually impressed" extension that earns the exceptional grade — and a clean upstream PR target since the gap is real and unfilled.

4. **Ensemble embedding** (🟢 lowest-effort win). Drop-in `embedding_func` swap, pre-trainable on Quora Question Pairs, benchmark hit precision/recall vs. single-model. Good as a secondary ablation axis or a fast first result.

5. **Adaptive per-cluster eviction budget** (🟡 most novel policy idea). Ada-KV's insight applied to topic clusters: measure per-cluster hit-rate variance, allocate eviction budget non-uniformly. Genuinely new vs. flat LRU/LFU — higher risk, higher originality.

6. **Hierarchical RAM+disk tier** (🟡 bigger build). CachedAttention + FlexGen + Mooncake: hot entries in RAM, cold spilled to disk, admission-controlled, cost-aware migration. Matches the syllabus's named "hierarchical RAM+disk cache" extension; more engineering, strong systems story.

### What to *not* spend time on
KV-tensor mechanics (KIVI, vAttention, PagedAttention internals, CacheBlend, TurboRAG) are 🔴 for a request-level cache — cite them for context, don't try to port them. WebCache (images) and Cache-me-if-you-can (task-specific distillation) are off-target. LMCache/KDN are only relevant if you deliberately switch to the harder KV-cache track.

### Suggested path
Baseline = **GPTCache**. Ship **#1 (composite cost-aware eviction)** as the rock-solid core (Correctness + Reproducibility + measurable gain), add **#2 (context-aware matching)** for a compounding, code-validated second result, and reach for **#3 (security-aware policy)** as the differentiated swing for the exceptional grade. Use **Caffeine's simulator** to pre-screen policies on your trace and W-TinyLFU as the baseline to beat. Every claim → an ablation in §5.

---

## Appendix — link corrections & notes
The syllabus links resolve cleanly except: several entries point at gateway pages whose canonical/arXiv versions are more useful — GenCache (`openreview MHGViOjZ27` → arXiv 2511.17565, **code** at sarthak-chakraborty/GenCache), Auditing Prompt Caching (`openreview gUj2fxQcLZ` → arXiv 2502.07776), InfiniGen (USENIX → arXiv 2406.19707), FlexGen (ACM → arXiv 2303.06865), ServerlessLLM (USENIX → arXiv 2401.14351), CacheBlend (ACM → arXiv 2405.16444). Papers named in the syllabus **without** a link, now resolved: TurboRAG (2410.07590), RAGCache (2404.12457), Mooncake (2407.00079), LeanKV (2412.03131). The two future-looking arXiv IDs (Key Collision 2601.23088, SCOLAP 2602.19811) are valid Jan/Feb 2026 submissions.
