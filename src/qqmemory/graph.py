"""Parameterized temporal property graph; all reads are tenant-and-scope bound."""

import time
import json
from neo4j import GraphDatabase
from qqbot.memory.policy import node_id, SINGLE, RELATIONS, portable, preference_slot
from .dossier import consolidate
from .lifecycle import visible, strength


class GraphStore:
    """Own Neo4j schema, evidence provenance, temporal revisions and audience-filtered views."""

    def __init__(self, uri):
        self.driver = GraphDatabase.driver(
            uri,
            auth=None,
            max_connection_pool_size=4,
            connection_acquisition_timeout=5,
            notifications_min_severity="WARNING",
        )
        self.driver.verify_connectivity()
        with self.driver.session() as s:
            s.run(
                "CREATE CONSTRAINT memory_id IF NOT EXISTS FOR (n:Memory) REQUIRE n.id IS UNIQUE"
            ).consume()
            s.run(
                "CREATE INDEX memory_scope IF NOT EXISTS FOR (n:Memory) ON (n.actor, n.scope)"
            ).consume()
            s.run(
                "CREATE CONSTRAINT dossier_id IF NOT EXISTS FOR (n:Dossier) REQUIRE n.id IS UNIQUE"
            ).consume()
            s.run(
                "CREATE CONSTRAINT revision_id IF NOT EXISTS FOR (n:Revision) REQUIRE n.id IS UNIQUE"
            ).consume()

    def ping(self):
        with self.driver.session() as s:
            return s.run("RETURN 1 AS ok").single()["ok"] == 1

    def forget(self, actor):
        """Delete only the selected actor graph, preserving unrelated accounts."""
        with self.driver.session() as s:
            s.run(
                "MATCH (n {actor:$actor}) WHERE n:Memory OR n:Dossier OR n:Revision DETACH DELETE n",
                actor=actor,
            ).consume()

    def prepare(self, actor, scope, event, extracted):
        """Derive stable source-linked nodes from validated user evidence, never assistant guesses."""
        created = event["created"]
        nodes, edges = [], []

        def add(kind, identity_key, text, **properties):
            """Persist idempotently with actor/scope keys and supersede older single-valued facts."""
            item = {
                "id": node_id(actor, scope, kind, identity_key),
                "actor": actor,
                "scope": scope,
                "kind": kind,
                "text": text,
                "created": created,
                "active": True,
                "access": 0,
                **properties,
            }
            nodes.append(item)
            return item["id"]

        def link(src, dst, relation):
            edges.append({"src": src, "dst": dst, "relation": RELATIONS[relation]})

        label = event.get("display_name", "")
        person = add(
            "person",
            "self",
            "当前说话者" + ("，QQ显示名：" + label if label else ""),
            display_name=label,
        )
        episode = add(
            "episode",
            event["id"],
            extracted["summary"],
            source=event["id"],
            question=event["question"],
            answer=event["answer"],
        )
        link(person, episode, "PARTICIPATED")
        for topic in extracted["topics"]:
            topic_id = add("topic", topic.casefold(), topic)
            link(episode, topic_id, "ABOUT")
        for claim in extracted["claims"]:
            labels = {
                "name": "用户自称姓名",
                "preferred_name": "用户希望的称呼",
                "city": "用户居住地",
                "occupation": "用户职业",
                "language": "用户语言",
                "interest": "用户兴趣",
                "project": "用户项目",
                "preference": "用户长期偏好",
                "goal": "用户目标",
                "todo": "用户待办计划",
                "commitment": "用户明确约定",
                "relationship": "用户表达的关系定位",
            }
            fact_identity = claim["key"] + "\0" + claim["value"]
            if claim["key"] in SINGLE:
                fact_identity += "\0" + event["id"]
            cid = add(
                claim["kind"],
                fact_identity,
                labels[claim["key"]] + "：" + claim["value"],
                value=claim["value"],
                key=claim["key"],
                evidence=claim["evidence"],
                source=event["id"],
                valid_from=created,
                valid_to=0,
                evidence_level="user_statement",
            )
            link(cid, person, "SUBJECT")
            link(cid, episode, "SUPPORTED_BY")
        return nodes, edges

    def add(self, actor, scope, nodes, edges):
        """Persist idempotently with actor/scope keys and supersede older single-valued facts."""
        with self.driver.session() as session:
            session.execute_write(self._add, actor, scope, nodes, edges)

    @staticmethod
    def _add(tx, actor, scope, nodes, edges):
        for node in nodes:
            if node.get("key") in SINGLE:
                old = tx.run(
                    """MATCH (n:Memory {actor:$actor, scope:$scope, key:$key})
                    WHERE n.id<>$id RETURN n.id AS id,n.valid_from AS since,n.active AS active""",
                    actor=actor,
                    scope=scope,
                    key=node["key"],
                    id=node["id"],
                ).data()
                newer = [n for n in old if n["since"] > node["valid_from"]]
                if newer:
                    node["active"] = False
                    node["valid_to"] = min(n["since"] for n in newer)
                else:
                    for n in old:
                        if n["active"]:
                            tx.run(
                                "MATCH (n:Memory {id:$id}) SET n.active=false,n.valid_to=$time",
                                id=n["id"],
                                time=node["valid_from"],
                            ).consume()
                            edges.append(
                                {
                                    "src": node["id"],
                                    "dst": n["id"],
                                    "relation": RELATIONS["SUPERSEDES"],
                                }
                            )
            tx.run(
                """MERGE (n:Memory {id:$id})
                ON CREATE SET n=$properties
                ON MATCH SET n.text=$properties.text,n.vector=$properties.vector,
                  n.evidence=CASE WHEN $properties.created>=coalesce(n.last_seen,n.created)
                    THEN coalesce($properties.evidence,n.evidence) ELSE n.evidence END,
                  n.source=CASE WHEN $properties.created>=coalesce(n.last_seen,n.created)
                    THEN coalesce($properties.source,n.source) ELSE n.source END,
                  n.display_name=CASE WHEN coalesce($properties.display_name,'')<>''
                    AND $properties.created>=coalesce(n.last_seen,n.created)
                    THEN $properties.display_name ELSE n.display_name END,
                  n.last_seen=CASE WHEN $properties.created>coalesce(n.last_seen,n.created)
                    THEN $properties.created ELSE coalesce(n.last_seen,n.created) END,
                  n.active=CASE WHEN coalesce(n.valid_to,0)>0 AND $properties.created>n.valid_to
                    AND $properties.active THEN true ELSE n.active END,
                  n.state=CASE WHEN coalesce(n.valid_to,0)>0 AND $properties.created>n.valid_to
                    AND $properties.active THEN 'open' ELSE n.state END
                """,
                id=node["id"],
                properties=node,
            ).consume()
        for edge in edges:
            tx.run(
                """MATCH (a:Memory {id:$src,actor:$actor,scope:$scope}),
                (b:Memory {id:$dst,actor:$actor,scope:$scope})
                MERGE (a)-[r:REL {relation:$relation}]->(b)
                SET r.actor=$actor,r.scope=$scope""",
                **edge,
                actor=actor,
                scope=scope,
            ).consume()
        tx.run(
            """MATCH (n:Memory {actor:$actor,scope:$scope}) WHERE n.kind IN ['fact','preference','task']
            OPTIONAL MATCH (n)-[:REL {relation:2}]->(e:Memory)
            WITH n,count(DISTINCT e) AS observations SET n.observations=observations""",
            actor=actor,
            scope=scope,
        ).consume()
        GraphStore._consolidate(tx, actor, scope)

    @staticmethod
    def _consolidate(tx, actor, scope):
        args = {"actor": actor, "scope": scope}
        person = tx.run(
            "MATCH (n:Memory {actor:$actor,scope:$scope,kind:'person'}) RETURN properties(n) AS node",
            **args,
        ).single()
        claims = tx.run(
            """MATCH (n:Memory {actor:$actor,scope:$scope,active:true})
            WHERE n.kind IN ['fact','preference','task']
            OPTIONAL MATCH (n)-[:REL {relation:2}]->(e:Memory {actor:$actor,scope:$scope,kind:'episode'})
            RETURN properties(n) AS node,collect({id:e.source,created:e.created}) AS proof,
                max(e.created) AS latest
            ORDER BY CASE WHEN node.key IN ['name','preferred_name','preference'] THEN 0 ELSE 1 END,
                latest DESC LIMIT 200""",
            **args,
        ).data()
        stats = tx.run(
            """MATCH (n:Memory {actor:$actor,scope:$scope,kind:'episode'})
            RETURN count(n) AS count,max(n.created) AS latest""",
            **args,
        ).single()
        topics = tx.run(
            """MATCH (e:Memory {actor:$actor,scope:$scope,kind:'episode'})
            -[:REL {relation:1}]->(t:Memory {actor:$actor,scope:$scope,kind:'topic'})
            WITH t,e ORDER BY e.created DESC
            WITH t,collect({source:e.source,text:e.text,recorded_at:e.created}) AS episodes
            WHERE size(episodes)>=2 RETURN t.text AS topic,size(episodes) AS observations,
                episodes[0..2] AS episodes ORDER BY observations DESC LIMIT 3""",
            **args,
        ).data()
        profile = consolidate(person["node"] if person else {}, claims, topics, stats["count"])
        tx.run(
            """MERGE (n:Dossier {id:$id}) SET n.actor=$actor,n.scope=$scope,
            n.payload=$payload,n.updated=$updated""",
            **args,
            id=node_id(actor, scope, "dossier", "self"),
            payload=json.dumps(profile, ensure_ascii=False),
            updated=stats["latest"] or 0,
        ).consume()
        return profile

    def profile(self, actor, scope):
        with self.driver.session() as s:
            record = s.run(
                "MATCH (n:Dossier {id:$id,actor:$actor,scope:$scope}) RETURN n.payload AS payload",
                id=node_id(actor, scope, "dossier", "self"),
                actor=actor,
                scope=scope,
            ).single()
            if record:
                return json.loads(record["payload"])
            # Upgrade existing graph memories lazily; never import old chat archives.
            exists = s.run(
                "MATCH (n:Memory {actor:$actor,scope:$scope}) RETURN count(n) AS count",
                actor=actor,
                scope=scope,
            ).single()["count"]
            return s.execute_write(self._consolidate, actor, scope) if exists else {}

    def snapshot(self, actor, scope, limit=800):
        with self.driver.session() as s:
            nodes = [
                r["node"]
                for r in s.run(
                    """MATCH (n:Memory {actor:$actor,scope:$scope,active:true})
                RETURN properties(n) AS node
                ORDER BY CASE WHEN n.kind='episode' THEN 0 ELSE 1 END DESC,n.created DESC LIMIT $limit""",
                    actor=actor,
                    scope=scope,
                    limit=limit,
                )
            ]
            ids = [n["id"] for n in nodes]
            edges = s.run(
                """MATCH (a:Memory {actor:$actor,scope:$scope})-[r:REL]->
                (b:Memory {actor:$actor,scope:$scope})
                WHERE a.id IN $ids AND b.id IN $ids
                RETURN a.id AS src,b.id AS dst,r.relation AS relation""",
                actor=actor,
                scope=scope,
                ids=ids,
            ).data()
        return nodes, edges

    def associate(self, actor, scope, source, target, similarity):
        # This edge records semantic relatedness only, not factual implication.
        with self.driver.session() as s:
            s.run(
                """MATCH (a:Memory {id:$source,actor:$actor,scope:$scope,kind:'topic'}),
                (b:Memory {id:$target,actor:$actor,scope:$scope,kind:'topic'})
                MERGE (a)-[r:REL {relation:5}]->(b) SET r.similarity=$similarity,
                r.actor=$actor,r.scope=$scope""",
                actor=actor,
                scope=scope,
                source=source,
                target=target,
                similarity=float(similarity),
            ).consume()

    def touch(self, actor, scope, ids):
        with self.driver.session() as s:
            s.run(
                """MATCH (n:Memory {actor:$actor,scope:$scope}) WHERE n.id IN $ids
                SET n.access=coalesce(n.access,0)+1,n.last_access=$now""",
                actor=actor,
                scope=scope,
                ids=ids,
                now=time.time(),
            ).consume()

    def view(self, actor, scope, private=False, mode="recall", limit=1200):
        # QQ account is the identity; scope is an audience boundary, not a person.
        """Filter by audience and lifecycle before exposing graph nodes to recall."""
        with self.driver.session() as s:
            rows = [
                r["node"]
                for r in s.run(
                    """MATCH (n:Memory {actor:$actor,active:true})
                WHERE $private OR n.scope=$scope OR n.key IN ['name','preferred_name','language','preference']
                RETURN properties(n) AS node ORDER BY
                    CASE WHEN n.key IN ['name','preferred_name','language','preference'] THEN 0
                         WHEN n.kind IN ['fact','task'] THEN 1 ELSE 2 END,
                    coalesce(n.last_seen,n.created) DESC LIMIT $limit""",
                    actor=actor,
                    scope=scope,
                    private=private,
                    limit=limit,
                )
            ]
            rows = [n for n in rows if private or n["scope"] == scope or portable(n)]
            if not private:
                rows = [
                    dict(
                        n,
                        cross_context=True,
                        behavior_slot=preference_slot(
                            n.get("value", "") + " " + n.get("evidence", "")
                        ),
                        evidence="（跨场景偏好，原文不向本群披露）",
                        question="",
                        answer="",
                    )
                    if n["scope"] != scope
                    else n
                    for n in rows
                ]
            latest = {}
            for n in rows:
                slot = (
                    n.get("key")
                    if n.get("key") in SINGLE
                    else (
                        "pref:"
                        + (
                            n.get("behavior_slot")
                            or preference_slot(n.get("value", "") + " " + n.get("evidence", ""))
                        )
                        if n.get("key") == "preference" and portable(n)
                        else ""
                    )
                )
                if slot and (
                    slot not in latest
                    or n.get("last_seen", n["created"])
                    > latest[slot].get("last_seen", latest[slot]["created"])
                ):
                    latest[slot] = n
            rows = [
                n for n in rows if n.get("key") not in SINGLE or latest[n["key"]]["id"] == n["id"]
            ]
            rows = [
                n
                for n in rows
                if not (n.get("key") == "preference" and portable(n))
                or latest[
                    "pref:"
                    + (
                        n.get("behavior_slot")
                        or preference_slot(n.get("value", "") + " " + n.get("evidence", ""))
                    )
                ]["id"]
                == n["id"]
            ]
            rows = visible(rows, mode)
            ids = [n["id"] for n in rows]
            edges = s.run(
                """MATCH (a:Memory {actor:$actor})-[r:REL]->(b:Memory {actor:$actor})
                WHERE a.id IN $ids AND b.id IN $ids RETURN a.id AS src,b.id AS dst,r.relation AS relation""",
                actor=actor,
                ids=ids,
            ).data()
        return rows, edges

    def audience_profile(self, actor, scope, private=False, mode="recall"):
        """Project account-level names/preferences without disclosing private source details to groups."""
        nodes, edges = self.view(actor, scope, private, mode)
        lookup = {n["id"]: n for n in nodes}
        claims = []
        for n in nodes:
            if n["kind"] not in ("fact", "preference", "task"):
                continue
            proof = [
                {"id": lookup[e["dst"]].get("source")}
                for e in edges
                if e["relation"] == 2 and e["src"] == n["id"]
            ]
            # Portable preferences carry their own quote, not the private episode.
            if not proof:
                proof = [{"id": n.get("source")}]
            claims.append({"node": n, "proof": proof, "latest": n.get("last_seen", n["created"])})
        persons = sorted(
            [n for n in nodes if n["kind"] == "person"],
            key=lambda n: n.get("last_seen", n["created"]),
            reverse=True,
        )
        topics = {}
        for edge in edges:
            if edge["relation"] != 1:
                continue
            e, t = lookup[edge["src"]], lookup[edge["dst"]]
            if e["kind"] == "episode":
                topics.setdefault(t["text"], []).append(
                    {"source": e.get("source"), "text": e["text"], "recorded_at": e["created"]}
                )
        experiences = [
            {
                "topic": t,
                "observations": len(es),
                "episodes": sorted(es, key=lambda e: e["recorded_at"], reverse=True)[:2],
            }
            for t, es in topics.items()
            if len(es) >= 2
        ][:3]
        result = consolidate(
            persons[0] if persons else {},
            claims,
            experiences,
            sum(n["kind"] == "episode" for n in nodes),
        )
        result.update(
            version=3,
            identity="qq_account",
            audience="private_all_own_contexts" if private else "group_plus_portable_preferences",
        )
        return result if nodes else {}

    def reinforce(self, actor, ids, useful):
        with self.driver.session() as s:
            s.run(
                """MATCH (n:Memory {actor:$actor,active:true}) WHERE n.id IN $ids
                SET n.useful=coalesce(n.useful,0)+$delta,
                n.reinforced_at=CASE WHEN $delta>0 THEN $now ELSE coalesce(n.reinforced_at,0) END""",
                actor=actor,
                ids=ids,
                delta=1 if useful else -1,
                now=time.time(),
            ).consume()

    def maintenance(self, now=None):
        now = time.time() if now is None else now
        with self.driver.session() as s:
            nodes = [
                r["node"]
                for r in s.run("MATCH (n:Memory {active:true}) RETURN properties(n) AS node")
            ]
            rows = [
                {"id": n["id"], "strength": strength(n, now), "dormant": strength(n, now) < 0.15}
                for n in nodes
            ]
            s.run(
                "UNWIND $rows AS row MATCH (n:Memory {id:row.id}) SET n.strength=row.strength,n.dormant=row.dormant",
                rows=rows,
            ).consume()
        return {"active": len(rows), "dormant": sum(r["dormant"] for r in rows)}

    def update(self, actor, scope, update, source, created, private=False):
        """Apply an evidenced revision to a unique visible target; retain tombstone provenance."""
        with self.driver.session() as s:
            return s.execute_write(self._update, actor, scope, update, source, created, private)

    @staticmethod
    def _update(tx, actor, scope, update, source, created, private):
        stable = {k: v for k, v in update.items() if k != "vector"}
        ident = node_id(
            actor,
            scope,
            "revision",
            source + json.dumps(stable, sort_keys=True, ensure_ascii=False),
        )
        prior = tx.run("MATCH (r:Revision {id:$id}) RETURN r.result AS result", id=ident).single()
        if prior:
            return json.loads(prior["result"])
        rows = [
            r["node"]
            for r in tx.run(
                """MATCH (n:Memory {actor:$actor,active:true})
            WHERE n.kind IN ['fact','preference','task'] AND ($private OR n.scope=$scope OR n.key IN ['name','preferred_name','language','preference'])
            RETURN properties(n) AS node""",
                actor=actor,
                scope=scope,
                private=private,
            )
        ]
        rows = [n for n in rows if private or n["scope"] == scope or portable(n)]
        if update.get("key"):
            rows = [n for n in rows if n.get("key") == update["key"]]
        if update["operation"] in ("complete", "cancel"):
            rows = [n for n in rows if n["kind"] == "task"]
        target = update["target"].strip().casefold()

        def matches(n):
            return (
                n["id"].startswith(target)
                if len(target) >= 8 and all(c in "0123456789abcdef" for c in target)
                else (
                    target == n.get("value", "").casefold()
                    or target == n["text"].casefold()
                    or target in n.get("value", "").casefold()
                )
            )

        matches_ = [n for n in rows if matches(n)]
        # Identical assertions in several scopes describe one value; unrelated matches require clarification.
        values = {(n.get("key"), n.get("value")) for n in matches_}
        if len(values) != 1:
            result = {
                "ok": False,
                "reason": "ambiguous" if values else "not_found",
                "matches": len(matches_),
            }
        elif any(n.get("last_seen", n["created"]) > created for n in matches_):
            result = {"ok": False, "reason": "older_than_fact"}
        else:
            op = update["operation"]
            ids = [n["id"] for n in matches_]
            if op in ("pin", "unpin"):
                tx.run(
                    "MATCH (n:Memory {actor:$actor}) WHERE n.id IN $ids SET n.pinned=$pinned",
                    actor=actor,
                    ids=ids,
                    pinned=op == "pin",
                ).consume()
            else:
                state = {
                    "complete": "completed",
                    "cancel": "cancelled",
                    "retract": "retracted",
                    "correct": "corrected",
                }[op]
                tx.run(
                    """MATCH (n:Memory {actor:$actor}) WHERE n.id IN $ids
                    SET n.active=false,n.state=$state,n.valid_to=$created,n.revision_source=$source
                    WITH n OPTIONAL MATCH (n)-[:REL {relation:2}]->(e:Memory)
                    SET e.invalidated=true""",
                    actor=actor,
                    ids=ids,
                    state=state,
                    created=created,
                    source=source,
                ).consume()
                if op == "correct":
                    old = matches_[0]
                    new = dict(
                        old,
                        id=node_id(actor, scope, old["kind"], source),
                        scope=scope,
                        value=update["value"],
                        text=old["text"].split("：")[0] + "：" + update["value"],
                        evidence=update["evidence"],
                        source=source,
                        created=created,
                        last_seen=created,
                        valid_from=created,
                        valid_to=0,
                        active=True,
                        state="open",
                        access=0,
                        vector=None,
                    )
                    # Encoding is performed before calling update; absent vector is excluded from neural recall until next ingestion.
                    if update.get("vector"):
                        new["vector"] = update["vector"]
                    tx.run(
                        "MERGE (n:Memory {id:$id}) SET n=$properties", id=new["id"], properties=new
                    ).consume()
                    eid = node_id(actor, scope, "episode", source)
                    tx.run(
                        """MERGE (e:Memory {id:$id}) ON CREATE SET e.actor=$actor,e.scope=$scope,e.kind='episode',
                        e.text=$text,e.question=$text,e.answer='',e.source=$source,e.created=$created,e.active=true,e.access=0""",
                        id=eid,
                        actor=actor,
                        scope=scope,
                        text=update["evidence"],
                        source=source,
                        created=created,
                    ).consume()
                    tx.run(
                        """MATCH (n:Memory {id:$id}),(e:Memory {id:$eid}) MERGE (n)-[r:REL {relation:2}]->(e)
                        SET r.actor=$actor,r.scope=$scope""",
                        id=new["id"],
                        eid=eid,
                        actor=actor,
                        scope=scope,
                    ).consume()
            result = {"ok": True, "operation": op, "ids": ids}
        tx.run(
            """CREATE (r:Revision {id:$id,actor:$actor,scope:$scope,created:$created,source:$source,evidence:$evidence,result:$result})""",
            id=ident,
            actor=actor,
            scope=scope,
            created=created,
            source=source,
            evidence=update["evidence"],
            result=json.dumps(result),
        ).consume()
        GraphStore._consolidate(tx, actor, scope)
        return result

    def status(self, actor, scope):
        with self.driver.session() as s:
            counts = s.run(
                """MATCH (n:Memory {actor:$actor,scope:$scope})
                RETURN n.kind AS kind,count(*) AS count""",
                actor=actor,
                scope=scope,
            ).data()
            facts = s.run(
                """MATCH (n:Memory {actor:$actor,scope:$scope,active:true})
                WHERE n.kind IN ['fact','preference','task']
                RETURN n.text AS text,n.created AS created ORDER BY n.created DESC LIMIT 8""",
                actor=actor,
                scope=scope,
            ).data()
        return {"counts": {r["kind"]: r["count"] for r in counts}, "facts": facts}
