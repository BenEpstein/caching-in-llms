# Copyright 2024-2025 The vLLM Production Stack Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import abc
import asyncio
import concurrent.futures
import enum
import math
import os  # LOADAWARE PATCH: beta is read from the environment
import random
import threading
import uuid
from typing import Dict, List, Optional

import requests
from fastapi import Request

try:
    from transformers import AutoTokenizer
except ImportError:
    pass

try:
    from lmcache.v1.cache_controller import controller_manager
    from lmcache.v1.cache_controller.message import (
        LookupMsg,
        QueryInstMsg,
    )
except ImportError:
    pass
from uhashring import HashRing

from vllm_router.log import init_logger
from vllm_router.service_discovery import EndpointInfo
from vllm_router.stats.engine_stats import EngineStats
from vllm_router.stats.request_stats import RequestStats
from vllm_router.utils import SingletonABCMeta

logger = init_logger(__name__)


class RoutingLogic(str, enum.Enum):
    ROUND_ROBIN = "roundrobin"
    SESSION_BASED = "session"
    KVAWARE = "kvaware"
    PREFIXAWARE = "prefixaware"
    DISAGGREGATED_PREFILL = "disaggregated_prefill"
    LOADAWARE = "loadaware"  # LOADAWARE PATCH: our placement policy


# LOADAWARE PATCH: the single tunable parameter of the `loadaware` placement
# policy. See `LoadAwareRouter` for the score it weights and for how it is
# overridden.
#
# There is no `alpha`. An argmax is invariant under positive scaling, so
# `alpha * benefit - beta * load` and `benefit - (beta/alpha) * load` are the
# same policy: alpha and beta were never two parameters, only their ratio.
#
# beta = 1.0 reads as: an endpoint sitting 100% above fleet-average load is
# docked one full cache hit's worth of preference. That statement mentions no
# hardware, model, request rate or fleet size, which is what makes it a
# defensible default rather than a number calibrated on one cluster.
DEFAULT_LOADAWARE_BETA = 1.0


def loadaware_param(env_name: str, override: Optional[float], default: float) -> float:
    """LOADAWARE PATCH: resolve one tunable — explicit kwarg > env var > default.

    The router's argument parser is in a different file (`parsers/parser.py`),
    so reading the environment keeps `loadaware` a **one-file** change and lets
    the value be retuned on a running deployment. The keyword argument is still
    honoured first so a future CLI flag needs no change here.
    """
    if override is not None:
        return float(override)
    raw = os.environ.get(env_name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning(f"Ignoring non-numeric {env_name}={raw!r}, using {default}")
        return default


class RoutingInterface(metaclass=SingletonABCMeta):

    def _qps_routing(
        self, endpoints: List[EndpointInfo], request_stats: Dict[str, RequestStats]
    ) -> str:
        """
        Route the request to the appropriate engine URL based on the QPS of
        each engine

        Args:
            endpoints (List[EndpointInfo]): The list of engine URLs
            request_stats (Dict[str, RequestStats]): The request stats
                indicating the request-level performance of each engine
        """
        lowest_qps = float("inf")
        ret = None
        for info in endpoints:
            url = info.url
            if url not in request_stats:
                return url  # This engine does not have any requests
            request_stat = request_stats[url]
            if request_stat.qps < lowest_qps:
                lowest_qps = request_stat.qps
                ret = url
        return ret

    def _update_hash_ring(self, endpoints: List["EndpointInfo"]):
        """
        Update the hash ring with the current list of endpoints.
        """
        # Extract endpoint URLs
        endpoint_urls = [endpoint.url for endpoint in endpoints]

        # Get the current nodes in the hash ring
        current_nodes = set(self.hash_ring.get_nodes())

        # Convert the new endpoint URLs to a set for easy comparison
        new_nodes = set(endpoint_urls)

        # Remove nodes that are no longer in the list
        for node in current_nodes - new_nodes:
            self.hash_ring.remove_node(node)

        # Add new nodes that are not already in the hash ring
        for node in new_nodes - current_nodes:
            self.hash_ring.add_node(node)

    def extract_session_id(self, request: Request, request_json: Dict) -> Optional[str]:
        """
        Extract the session id from the request headers or request body.
        """
        session_key = getattr(self, "session_key", None)
        if session_key is None:
            return None
        val = request.headers.get(session_key)
        return val if val is not None else request_json.get(session_key, None)

    @abc.abstractmethod
    def route_request(
        self,
        endpoints: List[EndpointInfo],
        engine_stats: Dict[str, EngineStats],
        request_stats: Dict[str, RequestStats],
        request: Request,
    ) -> str:
        """
        Route the request to the appropriate engine URL

        Args:
            endpoints (List[EndpointInfo]): The list of engine URLs
            engine_stats (Dict[str, EngineStats]): The engine stats indicating
                the 'physical' load of each engine
            request_stats (Dict[str, RequestStats]): The request stats
                indicating the request-level performance of each engine
            request (Request): The incoming request
        """
        raise NotImplementedError


class RoundRobinRouter(RoutingInterface):
    # TODO (ApostaC): when available engines in the endpoints changes, the
    # algorithm may not be "perfectly" round-robin.
    def __init__(self):
        if hasattr(self, "_initialized"):
            return
        self.req_id = 0
        self.sorted_endpoints = []
        self.last_endpoints_id = None
        self.last_endpoints_hash = None
        self._initialized = True

    def route_request(
        self,
        endpoints: List[EndpointInfo],
        engine_stats: Dict[str, EngineStats],
        request_stats: Dict[str, RequestStats],
        request: Request,
    ) -> str:
        """
        Route the request to the appropriate engine URL using a simple
        round-robin algorithm

        Args:
            endpoints (List[EndpointInfo]): The list of engine URLs
            engine_stats (Dict[str, EngineStats]): The engine stats indicating
                the 'physical' load of each engine
            request_stats (Dict[str, RequestStats]): The request stats
                indicating the request-level performance of each engine
            request (Request): The incoming request
        """
        endpoints_id = id(endpoints)
        if endpoints_id != self.last_endpoints_id:
            current_hash = hash(tuple(e.url for e in endpoints))
            if current_hash != self.last_endpoints_hash:
                self.sorted_endpoints = sorted(endpoints, key=lambda e: e.url)
                self.last_endpoints_hash = current_hash
            self.last_endpoints_id = endpoints_id
        chosen = self.sorted_endpoints[self.req_id % len(self.sorted_endpoints)]
        self.req_id += 1
        return chosen.url


class SessionRouter(RoutingInterface):
    """
    Route the request to the appropriate engine URL based on the session key
    in the request headers
    """

    def __init__(self, session_key: str = None):
        if hasattr(self, "_initialized"):
            return
        if session_key is None:
            raise ValueError("SessionRouter must be initialized with a session_key")
        self.session_key = session_key
        self.hash_ring = HashRing()
        self._initialized = True

    async def route_request(
        self,
        endpoints: List[EndpointInfo],
        engine_stats: Dict[str, EngineStats],
        request_stats: Dict[str, RequestStats],
        request: Request,
        request_json: Dict,
    ) -> str:
        """
        Route the request to the appropriate engine URL by the 'session id' in
        the request headers or request body.
        If there is no session id in the request header or request body, it will pick a server
        with lowest qps

        Args:
            endpoints (List[EndpointInfo]): The list of engine URLs
            engine_stats (Dict[str, EngineStats]): The engine stats indicating
                the 'physical' load of each engine
            request_stats (Dict[str, RequestStats]): The request stats
                indicating the request-level performance of each engine
            request (Request): The incoming request
            request_json (Dict): The request body (needed for finding the session id)
        """
        session_id = self.extract_session_id(request, request_json)
        logger.debug(f"Got session id: {session_id}")

        # Update the hash ring with the current list of endpoints
        self._update_hash_ring(endpoints)

        if session_id is None:
            # Route based on QPS if no session ID is present
            url = self._qps_routing(endpoints, request_stats)
        else:
            # Use the hash ring to get the endpoint for the session ID
            url = self.hash_ring.get_node(session_id)

        return url


class KvawareRouter(RoutingInterface):
    """
    Route the request to the appropriate engine URL by where the KV cache
    of the longest prefix match is found.
    """

    def __init__(
        self,
        lmcache_controller_port: int,
        session_key: str,
        kv_aware_threshold: int = 2000,
    ):
        self.lmcache_controller_port = lmcache_controller_port
        logger.info(
            f"Initializing KvawareRouter with port: {self.lmcache_controller_port}"
        )
        self.kv_manager = controller_manager.LMCacheControllerManager(
            {
                "pull": f"0.0.0.0:{self.lmcache_controller_port}",
                "reply": None,
            }
        )
        self.req_id = 0
        self.instance_id_to_ip = {}
        self.session_key = session_key
        self.hash_ring = HashRing()
        self.tokenizer = None
        self.threshold = kv_aware_threshold

    def start_kv_manager(self):
        """
        Start the kv manager
        """
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        self.lmcache_cluster_monitor_task = asyncio.run_coroutine_threadsafe(
            self.kv_manager.start_all(), self.loop
        )

    def query_manager(self, msg) -> str:
        """
        Get the instance id for the given message
        """
        instance_id = self.kv_manager.handle_orchestration_message(msg)
        return instance_id

    def close(self):
        """Gracefully shutdown the lmcache cluster monitor task."""
        if (
            hasattr(self, "lmcache_cluster_monitor_task")
            and self.lmcache_cluster_monitor_task
        ):
            logger.info("Shutting down lmcache cluster monitor task")
            self.lmcache_cluster_monitor_task.cancel()
            try:
                self.lmcache_cluster_monitor_task.result()
            except concurrent.futures.CancelledError:
                pass
            self.lmcache_cluster_monitor_task = None

    async def route_request(
        self,
        endpoints: List[EndpointInfo],
        engine_stats: Dict[str, EngineStats],
        request_stats: Dict[str, RequestStats],
        request: Request,
        request_json: Dict,
    ) -> str:
        """
        Route the request to the appropriate engine URL by where the KV cache
        of the longest prefix match is found.
        If there is no session id in the request header, it will pick a server
        with round robin.

        Args:
            endpoints (List[EndpointInfo]): The list of engine URLs
            engine_stats (Dict[str, EngineStats]): The engine stats indicating
               the 'physical' load of each engine
            request_stats (Dict[str, RequestStats]): The request stats
               indicating the request-level performance of each engine
            request (Request): The incoming request
            request_json (Dict): The request body (needed for finding the
            longest prefix match)
        """
        token_ids = None
        # Local-first tokenization, fall back to remote "/tokenize" API on failure
        # TODO (Yuhan): Handle chat completions
        try:
            if self.tokenizer is None:
                self.tokenizer = AutoTokenizer.from_pretrained(
                    endpoints[0].model_names[0]
                )
            token_ids = self.tokenizer.encode(request_json.get("prompt", ""))
        except Exception:
            # Remote /tokenize fallback (let errors bubble up to keep behavior simple)
            remote_url = endpoints[0].url + "/tokenize"
            headers = {"Content-Type": "application/json"}
            data = {
                "model": endpoints[0].model_names[0],
                "prompt": request_json.get("prompt", ""),
            }
            body = requests.post(
                remote_url, headers=headers, json=data, timeout=10
            ).json()
            token_ids = body["tokens"]

        event_id = "Lookup" + str(uuid.uuid4())
        msg = LookupMsg(tokens=token_ids, event_id=event_id)
        instance_id = await self.query_manager(msg)
        matched_tokens = math.inf
        logger.debug(f"Lookup return message: {instance_id}")
        if len(list(instance_id.layout_info.keys())) > 0:
            matched_instance_id = list(instance_id.layout_info.keys())[
                0
            ]  # Get the first key
            matched_tokens = instance_id.layout_info[matched_instance_id][1]

        if (
            instance_id is None
            or len(instance_id.layout_info) == 0
            or matched_tokens < max(len(token_ids) - self.threshold, 0)
        ):
            session_id = self.extract_session_id(request, request_json)
            logger.debug(f"Fallback to using session id: {session_id}")
            # Update the hash ring with the current list of endpoints
            self._update_hash_ring(endpoints)
            if session_id is None:
                # Route based on QPS if no session ID is present
                url = self._qps_routing(endpoints, request_stats)
            else:
                # Use the hash ring to get the endpoint for the session ID
                url = self.hash_ring.get_node(session_id)
            return url
        else:
            queried_instance_ids = [info for info in instance_id.layout_info]
            if queried_instance_ids[0] not in self.instance_id_to_ip:
                for endpoint in endpoints:
                    event_id = "QueryInst" + str(uuid.uuid4())
                    query_ip = endpoint.url.split(f":{endpoint.url.split(':')[-1]}")[
                        0
                    ].split("//")[1]
                    query_message = QueryInstMsg(
                        ip=query_ip,
                        event_id=event_id,
                    )
                    endpoint_instance_id = await self.query_manager(query_message)
                    logger.debug(
                        f"Query ip: {query_ip}, return instance id: {endpoint_instance_id}"
                    )
                    self.instance_id_to_ip[endpoint_instance_id.instance_id] = (
                        endpoint.url
                    )
                logger.info(f"Instance id to ip mapping: {self.instance_id_to_ip}")
            logger.info(
                f"Routing request to {queried_instance_ids[0]} found by kvaware router"
            )
            return self.instance_id_to_ip[queried_instance_ids[0]]


class LoadAwareRouter(KvawareRouter):
    """LOADAWARE PATCH: KV-cache-aware placement that also weighs live load.

    `kvaware` maximizes cache-hit benefit alone: it takes the *first* instance
    reported in `layout_info` and sends the request there however busy that
    instance is. `loadaware` scores **every** endpoint

        score(i) = matched_tokens(i) / prompt_tokens  -  beta * relative_load(i)

        relative_load(i) = (load(i) - mean_load) / max(1, mean_load)

    and routes to the argmax, so a warm-but-saturated instance can lose to a
    cold-but-idle one. It subclasses `KvawareRouter` and overrides only the
    selection step; `KvawareRouter` itself stays **byte-identical**, so
    `kvaware` behaviour is unchanged by this patch.

    Four deliberate departures from `kvaware`:

    1. **Benefit is normalized** to the fraction of the prompt already cached
       ([0, 1]) rather than a raw token count. Raw counts make beta scale with
       prompt length, so one beta would mean different policies for a 500- and
       a 4000-token prompt - unusable for a beta sensitivity sweep.
    2. **Load is normalized too**, against the fleet's own mean, and this is
       what lets a single beta ship. An absolute in-flight count has no bounded
       scale: it depends on request rate, prompt length and GPU, so the same
       beta is a different policy on every deployment. Concretely: a beta
       tuned where the busiest engine ran ~47 in-flight yields, on a fleet
       running 400, a load penalty far past the benefit term's cap of 1.0, so
       the cache stops mattering entirely and placement silently collapses to
       least-loaded - a failure with nothing in the logs to announce it. The
       denominator is clamped at 1 so that a fleet which is essentially idle
       reports no imbalance to act on: at mean load 0.2 a single in-flight
       request is not a 400%-overloaded engine.
    3. **Every endpoint is scored**, not only the holders in `layout_info`. An
       endpoint absent from `layout_info` scores benefit 0 - that is what makes
       "cold but idle beats warm but loaded" expressible at all.
    4. **`kv_aware_threshold` is not applied.** Upstream needs that band because
       it cannot weigh a small match against anything; the argmax can (a small
       match simply loses to load). Keeping the band would also route every
       sub-threshold prompt by QPS in *both* arms, turning that slice of the
       workload into an identical no-op comparison. The parameter is still
       accepted and forwarded to `KvawareRouter` for interface compatibility.

    The fallback is unchanged: when the Controller reports **no** holder at all,
    placement degrades to the upstream session-hash / QPS route.
    """

    def __init__(
        self,
        lmcache_controller_port: int,
        session_key: str,
        kv_aware_threshold: int = 2000,
        loadaware_beta: Optional[float] = None,
    ):
        super().__init__(
            lmcache_controller_port,
            session_key,
            kv_aware_threshold if kv_aware_threshold is not None else 2000,
        )
        #: Weight on the load penalty, in units of "full cache hits per 100%
        #: above fleet-average load". The benefit term carries an implicit
        #: weight of 1; see the module-level default for why there is no alpha.
        self.beta = loadaware_param(
            "LOADAWARE_BETA", loadaware_beta, DEFAULT_LOADAWARE_BETA
        )
        logger.info(f"Initialized LoadAwareRouter with beta={self.beta}")

    @staticmethod
    def load_penalty(request_stats: Dict[str, RequestStats], url: str) -> int:
        """In-flight requests on `url` — prefilling plus decoding.

        `request_stats` is the fresh, event-driven stats source (the scraped
        `engine_stats` lags by `--engine-stats-interval`). A URL missing from it
        has served no requests yet, which is load 0 — the same reading
        `_qps_routing` gives an unseen endpoint.
        """
        stats = request_stats.get(url) if request_stats else None
        if stats is None:
            return 0
        return stats.in_prefill_requests + stats.in_decoding_requests

    @classmethod
    def relative_loads(
        cls, request_stats: Dict[str, RequestStats], endpoints: List[EndpointInfo]
    ) -> Dict[str, float]:
        """Each endpoint's load as a signed fraction of the fleet mean.

        `(load - mean) / max(1, mean)`, so 0.0 is "average", +1.0 is "twice the
        fleet average" and -1.0 is "idle while the fleet is busy". The fleet
        mean is recomputed per request from the same live `request_stats` the
        raw counts come from, which is what makes `beta` self-calibrating: the
        router measures its own scale instead of inheriting one from whoever
        tuned it last.

        Clamping the denominator at 1 keeps a near-idle fleet quiet. Without it
        a mean of 0.1 turns one in-flight request into `relative_load = 9.0`,
        and the policy would thrash on noise at exactly the load level where
        there is nothing worth balancing.
        """
        loads = {
            endpoint.url: cls.load_penalty(request_stats, endpoint.url)
            for endpoint in endpoints
        }
        if not loads:
            return {}
        mean = sum(loads.values()) / len(loads)
        return {url: (load - mean) / max(1.0, mean) for url, load in loads.items()}

    def score_endpoint(
        self, matched_tokens: int, prompt_tokens: int, relative_load: float
    ) -> float:
        """`cache_hit_benefit - beta * relative_load` for one endpoint.

        Both terms are dimensionless: benefit is a fraction of *this prompt*,
        relative_load a fraction of *this fleet's* mean. So beta is a pure
        exchange rate between the two and carries no unit from the deployment.

        With two engines the arithmetic is worth stating. One engine at +r
        forces the other to -r, so the load gap is `2 * beta * r` and a full
        cache hit is exactly cancelled at `r = 1/(2*beta)`; at the default
        beta = 1.0 that crossover is r = 0.5. Larger fleets dilute the gap, so
        the per-endpoint reading in the module-level default is the one that
        generalizes.

        `matched_tokens` can come back LARGER than `prompt_tokens`: a
        ~2000-token prompt has been observed matching 2048 tokens, consistent
        with `layout_info` rounding a match up to LMCache's 256-token chunk
        boundary. That is what the `min()` guard is for - without it `benefit`
        would exceed 1.0 and outrank a genuine full hit.
        """
        benefit = min(matched_tokens, prompt_tokens) / max(prompt_tokens, 1)
        return benefit - self.beta * relative_load

    def matched_tokens_by_url(self, layout_info: Dict) -> Dict[str, int]:
        """Re-key the Controller's answer from instance_id to engine URL.

        One URL can carry two instance_ids: a restarted engine registers under a
        fresh id while the dead one lingers both in this bridge and in the
        Controller's `kv_pool`, which only drops an instance on an explicit
        deregister — so `lookup()` can still name the dead id as a holder.

        Only the **live** id may be credited: the restarted engine came back with
        an empty cache, so the dead id's match is phantom. Inverting the bridge
        resolves it — dicts preserve insertion order and `refresh_instance_map`
        appends ids as it learns them, so the last id written for a URL is the
        live one. Credit rides on that id alone; if it reports nothing, the URL
        scores no benefit, which is right once the bridge has caught up.

        **Residual window:** the bridge only learns the fresh id when it appears
        in some `layout_info`, i.e. after the restarted engine admits its first
        chunk. Until then the dead id is the only one mapped and its match still
        reads as credit. Closing that would mean an unconditional Controller
        round-trip per request on a path that already blocks the event loop
        (production-stack#1016), so the operational answer stands instead:
        verify the instance registry before a run and do not restart engines
        mid-run. `kvaware` has the same hole and routes purely on that credit.
        """
        url_to_instance = {url: iid for iid, url in self.instance_id_to_ip.items()}
        matched = {}
        for url, instance_id in url_to_instance.items():
            info = layout_info.get(instance_id)
            if info is not None:
                matched[url] = info[1]
        return matched

    def select_url(
        self,
        endpoints: List[EndpointInfo],
        request_stats: Dict[str, RequestStats],
        layout_info: Dict,
        prompt_tokens: int,
    ) -> Optional[str]:
        """The placement decision. Pure: no I/O, no awaits — see tests/.

        `layout_info` is keyed by instance_id and `request_stats` by engine URL;
        `self.instance_id_to_ip` bridges them, so it must be populated for
        *every* endpoint before this runs (`refresh_instance_map`).

        Ties are broken by lexicographic URL so that a run is reproducible;
        returns None if there is nothing to route to, which the caller turns
        into the upstream fallback.
        """
        matched_by_url = self.matched_tokens_by_url(layout_info)
        relative = self.relative_loads(request_stats, endpoints)
        best_url = None
        best_score = -math.inf
        for info in sorted(endpoints, key=lambda e: e.url):
            matched_tokens = matched_by_url.get(info.url, 0)
            relative_load = relative.get(info.url, 0.0)
            score = self.score_endpoint(matched_tokens, prompt_tokens, relative_load)
            logger.debug(
                f"loadaware score {info.url}: matched={matched_tokens}/{prompt_tokens} "
                f"load={self.load_penalty(request_stats, info.url)} "
                f"rel_load={relative_load:+.3f} score={score:.4f}"
            )
            if score > best_score:
                best_score = score
                best_url = info.url
        return best_url

    def instance_map_is_stale(self, endpoints: List[EndpointInfo], layout_info: Dict) -> bool:
        """Does the instance_id -> URL bridge still cover what we must score?

        Two ways it goes stale, and a count of entries catches neither, because
        the bridge only ever grows:

        - an **endpoint we cannot score**: some URL is not a value in the map,
          so its cache credit would read as 0 whatever it actually holds;
        - an **unknown holder**: `layout_info` names an instance_id the bridge
          has never seen — what an engine restart looks like, since the new
          process registers under a fresh id while the old one lingers here.

        Miss the second and placement silently degenerates to least-loaded for
        the life of the router, with nothing in the logs to say so.
        """
        mapped_urls = set(self.instance_id_to_ip.values())
        if any(endpoint.url not in mapped_urls for endpoint in endpoints):
            return True
        return any(
            instance_id not in self.instance_id_to_ip for instance_id in layout_info
        )

    async def refresh_instance_map(
        self, endpoints: List[EndpointInfo], layout_info: Dict
    ) -> None:
        """Populate instance_id -> URL for every endpoint, on demand.

        `KvawareRouter` builds this lazily and only far enough to translate the
        one instance it already picked; scoring needs the whole bridge.
        Each rebuild costs one awaited round-trip per endpoint to the Controller
        (production-stack#1016: this path blocks the event loop), so it must
        stay a once-per-fleet-change cost, not a per-request one — hence the
        `instance_map_is_stale` gate rather than an unconditional refresh.
        """
        if not self.instance_map_is_stale(endpoints, layout_info):
            return
        for endpoint in endpoints:
            event_id = "QueryInst" + str(uuid.uuid4())
            query_ip = endpoint.url.split(f":{endpoint.url.split(':')[-1]}")[0].split(
                "//"
            )[1]
            query_message = QueryInstMsg(ip=query_ip, event_id=event_id)
            endpoint_instance_id = await self.query_manager(query_message)
            logger.debug(
                f"Query ip: {query_ip}, return instance id: {endpoint_instance_id}"
            )
            self.instance_id_to_ip[endpoint_instance_id.instance_id] = endpoint.url
        logger.info(f"Instance id to ip mapping: {self.instance_id_to_ip}")

    def tokenize_prompt(self, endpoints: List[EndpointInfo], request_json: Dict) -> List[int]:
        """Local-first tokenization with the remote `/tokenize` fallback.

        Verbatim from `KvawareRouter.route_request`; lifted into a method here
        rather than refactored there, because that class must not change.
        """
        try:
            if self.tokenizer is None:
                self.tokenizer = AutoTokenizer.from_pretrained(
                    endpoints[0].model_names[0]
                )
            return self.tokenizer.encode(request_json.get("prompt", ""))
        except Exception:
            remote_url = endpoints[0].url + "/tokenize"
            headers = {"Content-Type": "application/json"}
            data = {
                "model": endpoints[0].model_names[0],
                "prompt": request_json.get("prompt", ""),
            }
            body = requests.post(remote_url, headers=headers, json=data, timeout=10).json()
            return body["tokens"]

    def fallback_url(
        self,
        endpoints: List[EndpointInfo],
        request_stats: Dict[str, RequestStats],
        request: Request,
        request_json: Dict,
    ) -> str:
        """Upstream's no-cache-info route: session hash if any, else lowest QPS."""
        session_id = self.extract_session_id(request, request_json)
        logger.debug(f"Fallback to using session id: {session_id}")
        self._update_hash_ring(endpoints)
        if session_id is None:
            return self._qps_routing(endpoints, request_stats)
        return self.hash_ring.get_node(session_id)

    async def route_request(
        self,
        endpoints: List[EndpointInfo],
        engine_stats: Dict[str, EngineStats],
        request_stats: Dict[str, RequestStats],
        request: Request,
        request_json: Dict,
    ) -> str:
        """
        Route the request to the engine with the best
        `cache_hit_benefit - beta * relative_load`.

        Args:
            endpoints (List[EndpointInfo]): The list of engine URLs
            engine_stats (Dict[str, EngineStats]): The engine stats indicating
               the 'physical' load of each engine. Unused: it is scrape-lagged,
               `request_stats` carries the live signal.
            request_stats (Dict[str, RequestStats]): The request stats
               indicating the request-level performance of each engine
            request (Request): The incoming request
            request_json (Dict): The request body (needed for the prefix match)
        """
        token_ids = self.tokenize_prompt(endpoints, request_json)

        event_id = "Lookup" + str(uuid.uuid4())
        msg = LookupMsg(tokens=token_ids, event_id=event_id)
        lookup_ret = await self.query_manager(msg)
        logger.debug(f"Lookup return message: {lookup_ret}")
        layout_info = getattr(lookup_ret, "layout_info", None) or {}

        if not layout_info:
            # Nothing cached anywhere — no benefit term to weigh.
            return self.fallback_url(endpoints, request_stats, request, request_json)

        await self.refresh_instance_map(endpoints, layout_info)
        url = self.select_url(endpoints, request_stats, layout_info, len(token_ids))
        if url is None:
            return self.fallback_url(endpoints, request_stats, request, request_json)
        logger.info(f"Routing request to {url} found by loadaware router")
        return url


class PrefixAwareRouter(RoutingInterface):
    """
    Route the request to the appropriate engine URL by where the longest
    prefix match is found.

    In this class, we assume that there is no eviction of prefix cache.
    """

    def __init__(self: int):
        if hasattr(self, "_initialized"):
            return
        from vllm_router.prefix.hashtrie import HashTrie

        self.hashtrie = HashTrie()
        self._initialized = True

    async def route_request(
        self,
        endpoints: List[EndpointInfo],
        engine_stats: Dict[str, EngineStats],
        request_stats: Dict[str, RequestStats],
        request: Request,
        request_json: Dict,
    ) -> str:
        """
        Route the request to the appropriate engine URL by where the longest
        prefix match is found.

        In this routing logic, we do not consider the eviction of prefix cache.

        Args:
            endpoints (List[EndpointInfo]): The list of engine URLs
            engine_stats (Dict[str, EngineStats]): The engine stats indicating
               the 'physical' load of each engine
            request_stats (Dict[str, RequestStats]): The request stats
               indicating the request-level performance of each engine
            request (Request): The incoming request
            request_json (Dict): The request body (needed for finding the
            longest prefix match)
        """

        # Handle chat completions
        if "messages" in request_json:
            # Get the last message from the messages array
            messages = request_json["messages"]
            if messages:
                # Concatenate all message content
                prompt_parts = []
                for message in messages:
                    content = message.get("content", "")
                    if isinstance(content, list):
                        # Handle multimodal messages
                        text_content = " ".join(
                            part.get("text", "")
                            for part in content
                            if part.get("type") == "text"
                        )
                        prompt_parts.append(text_content)
                    elif content is not None:
                        prompt_parts.append(content)
                prompt = "\n".join(prompt_parts)
            else:
                prompt = ""
        else:
            # Handle regular completions
            prompt = request_json["prompt"]

        available_endpoints = set(endpoint.url for endpoint in endpoints)
        _, matched_endpoint = await self.hashtrie.longest_prefix_match(
            prompt, available_endpoints
        )

        selected_endpoint = random.choice(list(matched_endpoint))

        await self.hashtrie.insert(prompt, selected_endpoint)

        return selected_endpoint


class DisaggregatedPrefillRouter(RoutingInterface):
    """
    Route the request to the appropriate engine URL by handling prefill and decode operations sequentially.
    First request goes to prefill endpoint, then second request goes to decode endpoint.
    """

    def __init__(self, prefill_model_labels: List[str], decode_model_labels: List[str]):
        self.prefill_model_labels = prefill_model_labels
        self.decode_model_labels = decode_model_labels
        self.request_cache = {}  # Cache to store prefill results

    def route_request(
        self,
        endpoints: List[EndpointInfo],
        engine_stats: Dict[str, EngineStats],
        request_stats: Dict[str, RequestStats],
        request: Request,
        request_json: Dict,
    ) -> str:
        """
        Route the request to appropriate endpoints for prefill and decode operations.
        First request goes to prefill endpoint, then second request goes to decode endpoint.
        """
        # Find prefill and decode endpoints
        is_prefill = request_json.get("max_tokens", 0) == 1
        if is_prefill:
            logger.info("Prefill request")
        else:
            logger.info("Decode request")

        # Find endpoints with matching model labels
        prefiller_endpoints = [
            e for e in endpoints if e.model_label in self.prefill_model_labels
        ]
        decoder_endpoints = [
            e for e in endpoints if e.model_label in self.decode_model_labels
        ]
        if is_prefill:
            return prefiller_endpoints[0].url
        else:
            return decoder_endpoints[0].url


# Instead of managing a global _global_router, we can define the initialization functions as:
def initialize_routing_logic(
    routing_logic: RoutingLogic, *args, **kwargs
) -> RoutingInterface:
    if routing_logic == RoutingLogic.ROUND_ROBIN:
        logger.info("Initializing round-robin routing logic")
        return RoundRobinRouter()
    elif routing_logic == RoutingLogic.SESSION_BASED:
        logger.info(f"Initializing session-based routing logic with kwargs: {kwargs}")
        return SessionRouter(kwargs.get("session_key"))
    elif routing_logic == RoutingLogic.KVAWARE:
        logger.info("Initializing kvaware routing logic")
        router = KvawareRouter(
            kwargs.get("lmcache_controller_port"),
            kwargs.get("session_key"),
            kwargs.get("kv_aware_threshold"),
        )
        router.start_kv_manager()
        return router
    elif routing_logic == RoutingLogic.LOADAWARE:
        # LOADAWARE PATCH: beta is passed through when present; absent, the
        # router falls back to LOADAWARE_BETA in the environment and then to
        # the documented default - note `kwargs.get(...)` yields None, so the
        # default must live in the callee, not in the signature.
        logger.info("Initializing loadaware routing logic")
        router = LoadAwareRouter(
            kwargs.get("lmcache_controller_port"),
            kwargs.get("session_key"),
            kwargs.get("kv_aware_threshold"),
            kwargs.get("loadaware_beta"),
        )
        router.start_kv_manager()
        return router
    elif routing_logic == RoutingLogic.PREFIXAWARE:
        logger.info("Initializing prefix-aware routing logic")
        return PrefixAwareRouter()
    elif routing_logic == RoutingLogic.DISAGGREGATED_PREFILL:
        logger.info("Initializing disaggregated prefill routing logic")
        return DisaggregatedPrefillRouter(
            kwargs.get("prefill_model_labels"), kwargs.get("decode_model_labels")
        )
    else:
        raise ValueError(f"Invalid routing logic {routing_logic}")


def reconfigure_routing_logic(
    routing_logic: RoutingLogic, *args, **kwargs
) -> RoutingInterface:
    # Remove the existing routers from the singleton registry
    cleanup_routing_logic()
    return initialize_routing_logic(routing_logic, *args, **kwargs)


def get_routing_logic() -> RoutingInterface:
    # Look up in our singleton registry which router (if any) has been created.
    for cls in (
        SessionRouter,
        RoundRobinRouter,
        KvawareRouter,
        LoadAwareRouter,  # LOADAWARE PATCH
        PrefixAwareRouter,
        DisaggregatedPrefillRouter,
    ):
        if cls in SingletonABCMeta._instances:
            return cls()
    raise ValueError("The global router has not been initialized")


def cleanup_routing_logic():
    """Clean up all routing logic instances."""
    for cls in (
        SessionRouter,
        RoundRobinRouter,
        KvawareRouter,
        LoadAwareRouter,  # LOADAWARE PATCH
        PrefixAwareRouter,
        DisaggregatedPrefillRouter,
    ):
        if cls in SingletonABCMeta._instances:
            instance = cls()
            if hasattr(instance, "close"):
                instance.close()
            del SingletonABCMeta._instances[cls]
