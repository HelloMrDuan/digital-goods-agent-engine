-- Schema only from main bf5e7ffa; no production data.
CREATE TABLE ai_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                operation TEXT NOT NULL,
                entity_type TEXT NOT NULL,
                entity_id INTEGER,
                provider TEXT NOT NULL,
                model TEXT NOT NULL,
                status TEXT NOT NULL,
                prompt_tokens INTEGER NOT NULL DEFAULT 0,
                completion_tokens INTEGER NOT NULL DEFAULT 0,
                total_tokens INTEGER NOT NULL DEFAULT 0,
                estimated_cost_cny REAL NOT NULL DEFAULT 0,
                error TEXT,
                created_at TEXT NOT NULL
            );
CREATE TABLE analysis_tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                opportunity_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'PENDING',
                progress INTEGER NOT NULL DEFAULT 0,
                stage TEXT,
                message TEXT,
                result_json TEXT,
                error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
CREATE TABLE cluster_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                status TEXT NOT NULL,
                method TEXT NOT NULL,
                window_days INTEGER NOT NULL,
                threshold REAL NOT NULL,
                evidence_count INTEGER NOT NULL DEFAULT 0,
                cluster_count INTEGER NOT NULL DEFAULT 0,
                cross_platform_cluster_count INTEGER NOT NULL DEFAULT 0,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                error TEXT
            );
CREATE TABLE collection_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id INTEGER NOT NULL,
                status TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                fetched_count INTEGER NOT NULL DEFAULT 0,
                inserted_count INTEGER NOT NULL DEFAULT 0,
                duplicate_count INTEGER NOT NULL DEFAULT 0,
                rejected_count INTEGER NOT NULL DEFAULT 0,
                retry_count INTEGER NOT NULL DEFAULT 0,
                duration_ms INTEGER NOT NULL DEFAULT 0,
                error TEXT,
                FOREIGN KEY(source_id) REFERENCES sources(id) ON DELETE CASCADE
            );
CREATE TABLE deliveries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id INTEGER NOT NULL,
                platform TEXT,
                order_ref TEXT NOT NULL,
                buyer_ref TEXT,
                delivery_uri TEXT,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(platform, order_ref),
                FOREIGN KEY(product_id) REFERENCES products(id) ON DELETE RESTRICT
            );
CREATE TABLE evidence (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id INTEGER NOT NULL,
                platform TEXT NOT NULL,
                source_type TEXT NOT NULL,
                source_item_id TEXT,
                source_url TEXT,
                title TEXT NOT NULL,
                content TEXT,
                author TEXT,
                published_at TEXT,
                collected_at TEXT NOT NULL,
                engagement_json TEXT,
                raw_json TEXT NOT NULL,
                raw_hash TEXT NOT NULL,
                canonical_key TEXT NOT NULL UNIQUE,
                content_fingerprint TEXT,
                evidence_quality_score REAL NOT NULL DEFAULT 0,
                published_time_quality TEXT NOT NULL DEFAULT 'fallback',
                demand_signal_score REAL NOT NULL DEFAULT 0,
                content_kind TEXT NOT NULL DEFAULT 'post',
                parent_source_item_id TEXT,
                thread_url TEXT,
                intent_class TEXT NOT NULL DEFAULT 'unclassified',
                intent_confidence REAL NOT NULL DEFAULT 0,
                intent_weight REAL NOT NULL DEFAULT 0,
                intent_reason_json TEXT, last_seen_at TEXT, observation_count INTEGER NOT NULL DEFAULT 1,
                FOREIGN KEY(source_id) REFERENCES sources(id) ON DELETE CASCADE
            );
CREATE TABLE evidence_observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                evidence_id INTEGER NOT NULL,
                source_id INTEGER NOT NULL,
                observed_at TEXT NOT NULL,
                engagement_json TEXT,
                raw_hash TEXT NOT NULL,
                FOREIGN KEY(evidence_id) REFERENCES evidence(id) ON DELETE CASCADE,
                FOREIGN KEY(source_id) REFERENCES sources(id) ON DELETE CASCADE
            );
CREATE TABLE evidence_sources (
                evidence_id INTEGER NOT NULL,
                source_id INTEGER NOT NULL,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                observation_count INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY(evidence_id, source_id),
                FOREIGN KEY(evidence_id) REFERENCES evidence(id) ON DELETE CASCADE,
                FOREIGN KEY(source_id) REFERENCES sources(id) ON DELETE CASCADE
            );
CREATE TABLE opportunities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                candidate_key TEXT NOT NULL UNIQUE,
                normalized_need TEXT,
                category TEXT,
                status TEXT NOT NULL DEFAULT 'NEW',
                demand_score INTEGER,
                purchase_intent INTEGER,
                competition INTEGER,
                production_cost INTEGER,
                after_sales_cost INTEGER,
                copyright_risk INTEGER,
                automation_fit INTEGER,
                overall_score INTEGER,
                explicit_demand_count INTEGER NOT NULL DEFAULT 0,
                probable_demand_count INTEGER NOT NULL DEFAULT 0,
                comment_demand_count INTEGER NOT NULL DEFAULT 0,
                suggested_product TEXT,
                ai_reason TEXT,
                evidence_count INTEGER NOT NULL DEFAULT 0,
                analysis_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            , cluster_method TEXT, cluster_signature TEXT, platform_count INTEGER NOT NULL DEFAULT 0, source_count INTEGER NOT NULL DEFAULT 0, first_seen_at TEXT, last_seen_at TEXT, current_7d INTEGER NOT NULL DEFAULT 0, previous_7d INTEGER NOT NULL DEFAULT 0, growth_7d_pct REAL, trend_score REAL, trend_updated_at TEXT, analysis_stale INTEGER NOT NULL DEFAULT 0, support_evidence_count INTEGER NOT NULL DEFAULT 0, support_platform_count INTEGER NOT NULL DEFAULT 0, trend_confidence REAL NOT NULL DEFAULT 0, trusted_trend_score REAL NOT NULL DEFAULT 0, acceleration_pct REAL, effective_signal_count INTEGER NOT NULL DEFAULT 0, data_quality_score REAL NOT NULL DEFAULT 0, cluster_active INTEGER NOT NULL DEFAULT 1, last_cluster_run_id INTEGER);
CREATE TABLE opportunity_evidence (
                opportunity_id INTEGER NOT NULL,
                evidence_id INTEGER NOT NULL,
                relation_type TEXT NOT NULL DEFAULT 'demand',
                PRIMARY KEY(opportunity_id, evidence_id),
                FOREIGN KEY(opportunity_id) REFERENCES opportunities(id) ON DELETE CASCADE,
                FOREIGN KEY(evidence_id) REFERENCES evidence(id) ON DELETE CASCADE
            );
CREATE TABLE products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                opportunity_id INTEGER NOT NULL,
                sku TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                category TEXT,
                audience TEXT,
                price_suggestion REAL,
                title TEXT,
                description TEXT,
                keywords_json TEXT,
                deliverables_json TEXT,
                faq_json TEXT,
                compliance_notes TEXT,
                status TEXT NOT NULL DEFAULT 'DRAFT',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(opportunity_id) REFERENCES opportunities(id) ON DELETE RESTRICT
            );
CREATE TABLE sources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                source_type TEXT NOT NULL,
                platform TEXT NOT NULL,
                query TEXT,
                url TEXT,
                config_json TEXT,
                enabled INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'NOT_CHECKED',
                last_error TEXT,
                last_checked_at TEXT,
                last_success_at TEXT,
                last_item_count INTEGER NOT NULL DEFAULT 0,
                failure_streak INTEGER NOT NULL DEFAULT 0,
                empty_streak INTEGER NOT NULL DEFAULT 0,
                next_retry_at TEXT,
                avg_duration_ms REAL NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(name)
            );
CREATE TABLE trend_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                opportunity_id INTEGER NOT NULL,
                calculated_at TEXT NOT NULL,
                evidence_total INTEGER NOT NULL,
                platform_count INTEGER NOT NULL,
                source_count INTEGER NOT NULL,
                unique_authors INTEGER NOT NULL DEFAULT 0,
                current_7d INTEGER NOT NULL DEFAULT 0,
                previous_7d INTEGER NOT NULL DEFAULT 0,
                current_30d INTEGER NOT NULL DEFAULT 0,
                growth_7d_pct REAL,
                is_new INTEGER NOT NULL DEFAULT 0,
                raw_engagement_total REAL NOT NULL DEFAULT 0,
                trend_score REAL NOT NULL DEFAULT 0,
                trend_confidence REAL NOT NULL DEFAULT 0,
                trusted_trend_score REAL NOT NULL DEFAULT 0,
                current_3d INTEGER NOT NULL DEFAULT 0,
                prior_4d INTEGER NOT NULL DEFAULT 0,
                acceleration_pct REAL,
                effective_signal_count INTEGER NOT NULL DEFAULT 0,
                timestamp_coverage_pct REAL NOT NULL DEFAULT 0,
                support_evidence_count INTEGER NOT NULL DEFAULT 0,
                support_platform_count INTEGER NOT NULL DEFAULT 0,
                explicit_demand_count INTEGER NOT NULL DEFAULT 0,
                probable_demand_count INTEGER NOT NULL DEFAULT 0,
                comment_demand_count INTEGER NOT NULL DEFAULT 0,
                timeline_json TEXT NOT NULL,
                platform_breakdown_json TEXT NOT NULL,
                source_breakdown_json TEXT NOT NULL,
                FOREIGN KEY(opportunity_id) REFERENCES opportunities(id) ON DELETE CASCADE
            );
CREATE INDEX idx_evidence_fingerprint ON evidence(content_fingerprint);
CREATE INDEX idx_evidence_intent ON evidence(intent_class, platform, published_at);
CREATE INDEX idx_evidence_obs ON evidence_observations(evidence_id, observed_at DESC);
CREATE INDEX idx_evidence_platform ON evidence(platform, collected_at DESC);
CREATE INDEX idx_evidence_source ON evidence(source_id, collected_at DESC);
CREATE INDEX idx_evidence_thread ON evidence(platform, thread_url);
CREATE INDEX idx_trend_opp_time ON trend_snapshots(opportunity_id, calculated_at DESC);
