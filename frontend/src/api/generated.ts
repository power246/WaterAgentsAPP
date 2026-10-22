// Generated from the current backend OpenAPI. Run npm run generate:api.
export interface paths {
    "/api/admin/history-cleanups": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Execute History */
        post: operations["execute_history_api_admin_history_cleanups_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/admin/history-cleanups/preview": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Preview History */
        post: operations["preview_history_api_admin_history_cleanups_preview_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/admin/operations": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** List Operations */
        get: operations["list_operations_api_admin_operations_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/admin/operations/{operation_id}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Get Operation */
        get: operations["get_operation_api_admin_operations__operation_id__get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/admin/rag": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Get Rag */
        get: operations["get_rag_api_admin_rag_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/admin/rag/generations/{generation_id}/deletion-preview": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Preview Deletion */
        post: operations["preview_deletion_api_admin_rag_generations__generation_id__deletion_preview_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/admin/rag/generations/{generation_id}/deletions": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Execute Deletion */
        post: operations["execute_deletion_api_admin_rag_generations__generation_id__deletions_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/admin/rag/generations/{generation_id}/failed-jobs/retries": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Execute Retry */
        post: operations["execute_retry_api_admin_rag_generations__generation_id__failed_jobs_retries_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/admin/rag/generations/{generation_id}/failed-jobs/retry-preview": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Preview Retry */
        post: operations["preview_retry_api_admin_rag_generations__generation_id__failed_jobs_retry_preview_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/admin/rag/profile-handoffs": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Execute Profile */
        post: operations["execute_profile_api_admin_rag_profile_handoffs_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/admin/rag/profile-handoffs/preview": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Preview Profile */
        post: operations["preview_profile_api_admin_rag_profile_handoffs_preview_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/admin/storage": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Get Storage */
        get: operations["get_storage_api_admin_storage_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/analysis/runs": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** List Runs */
        get: operations["list_runs_api_analysis_runs_get"];
        put?: never;
        /** Create Run */
        post: operations["create_run_api_analysis_runs_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/analysis/runs/{run_id}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Get Run */
        get: operations["get_run_api_analysis_runs__run_id__get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/analysis/runs/{run_id}/cancel": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Cancel Run */
        post: operations["cancel_run_api_analysis_runs__run_id__cancel_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/analysis/runs/{run_id}/events": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * Subscribe Events
         * @description Sends the latest complete lightweight run snapshot, then replacement snapshots when source state changes. Last-Event-ID is ignored. Normal completion emits stream_end; disconnecting does not cancel the run.
         */
        get: operations["subscribe_events_api_analysis_runs__run_id__events_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/analysis/runs/{run_id}/persistence/retry": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Retry Persistence */
        post: operations["retry_persistence_api_analysis_runs__run_id__persistence_retry_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/analysis/runs/{run_id}/sessions/{session_id}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Get Session */
        get: operations["get_session_api_analysis_runs__run_id__sessions__session_id__get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/analysis/runs/{run_id}/sessions/{session_id}/events": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * Subscribe Session Events
         * @description Replays visible Agent-session process events after the session-scoped integer Last-Event-ID, then streams new events. Normal completion emits stream_end.
         */
        get: operations["subscribe_session_events_api_analysis_runs__run_id__sessions__session_id__events_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/analysis/runs/{run_id}/sessions/{session_id}/transcript": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Get Session Transcript */
        get: operations["get_session_transcript_api_analysis_runs__run_id__sessions__session_id__transcript_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/analysis/runs/{run_id}/transcript/{item_id}/anchor-distance-estimates": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Get Anchor Distance Estimates */
        get: operations["get_anchor_distance_estimates_api_analysis_runs__run_id__transcript__item_id__anchor_distance_estimates_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/analysis/runs/{run_id}/transcript/{item_id}/content": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Get Transcript Content */
        get: operations["get_transcript_content_api_analysis_runs__run_id__transcript__item_id__content_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/guidelines": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** List Guidelines */
        get: operations["list_guidelines_api_guidelines_get"];
        put?: never;
        /** Create Guideline */
        post: operations["create_guideline_api_guidelines_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/guidelines/operations/{operation_id}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Get Guideline Operation */
        get: operations["get_guideline_operation_api_guidelines_operations__operation_id__get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/guidelines/operations/{operation_id}/retry": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Retry Guideline Operation */
        post: operations["retry_guideline_operation_api_guidelines_operations__operation_id__retry_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/guidelines/{document_id}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Get Guideline */
        get: operations["get_guideline_api_guidelines__document_id__get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/guidelines/{document_id}/draft": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        /** Save Guideline Draft */
        put: operations["save_guideline_draft_api_guidelines__document_id__draft_put"];
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/guidelines/{document_id}/local-edit": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Local Edit Guideline */
        post: operations["local_edit_guideline_api_guidelines__document_id__local_edit_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/guidelines/{document_id}/publish": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Publish Guideline */
        post: operations["publish_guideline_api_guidelines__document_id__publish_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/guidelines/{document_id}/restore-bundled": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Restore Bundled Guideline */
        post: operations["restore_bundled_guideline_api_guidelines__document_id__restore_bundled_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/guidelines/{document_id}/set-enabled": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Set Guideline Enabled */
        post: operations["set_guideline_enabled_api_guidelines__document_id__set_enabled_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/guidelines/{document_id}/versions/{version_id}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Get Guideline Version */
        get: operations["get_guideline_version_api_guidelines__document_id__versions__version_id__get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/keys": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** List Keys */
        get: operations["list_keys_api_keys_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/keys/{key_name}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        /** Save Key */
        put: operations["save_key_api_keys__key_name__put"];
        post?: never;
        /** Delete Key */
        delete: operations["delete_key_api_keys__key_name__delete"];
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/keys/{key_name}/test": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Test Key */
        post: operations["test_key_api_keys__key_name__test_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/settings": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Get Settings */
        get: operations["get_settings_api_settings_get"];
        /** Replace Settings */
        put: operations["replace_settings_api_settings_put"];
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/settings/unit-policies": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Confirm Unit Policy */
        post: operations["confirm_unit_policy_api_settings_unit_policies_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/settings/unit-policies/preview": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Preview Unit Policy */
        post: operations["preview_unit_policy_api_settings_unit_policies_preview_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/health": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Health */
        get: operations["health_health_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
}
export type webhooks = Record<string, never>;
export interface components {
    schemas: {
        /**
         * AdminActionKind
         * @enum {string}
         */
        AdminActionKind: "history_cleanup" | "profile_handoff" | "retry_failed_jobs" | "generation_deletion";
        /** AdminConfirmation */
        AdminConfirmation: {
            /**
             * Expires At
             * Format: date-time
             */
            expires_at: string;
            /** Required Phrase */
            required_phrase: string;
            /** Token */
            token: string;
        };
        /** AdminOperationListResponse */
        AdminOperationListResponse: {
            /** Items */
            items: components["schemas"]["AdminOperationSnapshot"][];
            /** Next Cursor */
            next_cursor: string | null;
        };
        /** AdminOperationSnapshot */
        AdminOperationSnapshot: {
            /**
             * Created At
             * Format: date-time
             */
            created_at: string;
            /**
             * Finished At
             * @default null
             */
            finished_at: string | null;
            kind: components["schemas"]["AdminActionKind"];
            /**
             * Operation Id
             * Format: uuid
             */
            operation_id: string;
            progress: components["schemas"]["AdminProgress"];
            /** Resource Identity */
            resource_identity: string;
            /** @default null */
            safe_error: components["schemas"]["AdminSafeError"] | null;
            /**
             * Safe Result
             * @default null
             */
            safe_result: (components["schemas"]["HistoryCleanupResult"] | components["schemas"]["ProfileHandoffResult"] | components["schemas"]["RetryFailedJobsResult"] | components["schemas"]["GenerationDeletionResult"]) | null;
            /**
             * Started At
             * @default null
             */
            started_at: string | null;
            state: components["schemas"]["AdminOperationState"];
        };
        /**
         * AdminOperationState
         * @enum {string}
         */
        AdminOperationState: "pending" | "running" | "succeeded" | "failed" | "interrupted";
        /** AdminProgress */
        AdminProgress: {
            /**
             * Failed Items
             * @default 0
             */
            failed_items: number;
            /**
             * Processed Items
             * @default 0
             */
            processed_items: number;
            /**
             * Succeeded Items
             * @default 0
             */
            succeeded_items: number;
            /**
             * Total Items
             * @default null
             */
            total_items: number | null;
        };
        /** AdminRAGStatusResponse */
        AdminRAGStatusResponse: {
            handoff: components["schemas"]["RAGProfileHandoffState"];
            index: components["schemas"]["RAGIndexHealth"];
        };
        /** AdminSafeError */
        AdminSafeError: {
            /** Code */
            code: string;
            /** Message */
            message: string;
            /**
             * Retryable
             * @default false
             */
            retryable: boolean;
        };
        /** AdminStorageStatusResponse */
        AdminStorageStatusResponse: {
            credential_store: components["schemas"]["StorageItemStatus"];
            evidence_cache: components["schemas"]["StorageItemStatus"];
            history: components["schemas"]["StorageItemStatus"];
            rag_index: components["schemas"]["StorageItemStatus"];
        };
        /**
         * AgentRole
         * @enum {string}
         */
        AgentRole: "query_parser" | "waterbody" | "run_summarizer";
        /**
         * AgentSessionStatus
         * @enum {string}
         */
        AgentSessionStatus: "pending" | "running" | "completed" | "failed" | "cancelled";
        /**
         * AggregationMethod
         * @enum {string}
         */
        AggregationMethod: "mean" | "median" | "min" | "max" | "sum" | "p90" | "latest";
        /** AllHistorySelection */
        AllHistorySelection: {
            /**
             * @description discriminator enum property added by openapi-typescript
             * @enum {string}
             */
            kind: "all_history";
        };
        /** AnalysisConfigurationSnapshot */
        AnalysisConfigurationSnapshot: {
            /** Analysis Timezone */
            analysis_timezone: string;
            memory_mode: components["schemas"]["MemoryMode"];
            parser_model: components["schemas"]["ModelSelection"];
            time_resolution_preference: components["schemas"]["TimeResolutionPreference"];
            time_resolution_preference_source: components["schemas"]["TimeResolutionPreferenceSource"];
            /**
             * Unit Policy Version
             * @default legacy
             */
            unit_policy_version: number | "legacy";
            /**
             * Unit Registry Digest
             * @default unknown
             */
            unit_registry_digest: string | "unknown";
            waterbody_model: components["schemas"]["ModelSelection"];
        };
        /** AnalysisContext */
        AnalysisContext: {
            /** Analysis Timezone */
            analysis_timezone: string;
            /**
             * Decision Instant
             * Format: date-time
             */
            decision_instant: string;
            evidence_rule_plan?: components["schemas"]["EvidenceRulePlan"];
            memory_mode: components["schemas"]["MemoryMode"];
            mode: components["schemas"]["InferenceMode"];
            /**
             * Params Used
             * @description One to six user target parameters from the 154-item capability baseline, including explicit/inferred source and rationale.
             */
            params_used: components["schemas"]["ResolvedParameterIntent"][];
            parser_model: components["schemas"]["ModelSelection"];
            /** Query */
            query: string;
            requested_date_range: components["schemas"]["LocalDateRange"];
            /**
             * Schema Version
             * @default 1
             * @constant
             */
            schema_version: 1;
            /**
             * Supporting Params
             * @description System support parameters kept separate from user targets; the execution plan always includes USGS 00060 discharge.
             */
            supporting_params: components["schemas"]["SupportingParameterIntent"][];
            target: components["schemas"]["ResolvedTarget"];
            time_range: components["schemas"]["TimeRange"];
            time_resolution: components["schemas"]["TimeResolution"];
            time_resolution_source: components["schemas"]["TimeResolutionSource"];
            /** Time Segments */
            time_segments: components["schemas"]["AnalysisTimeSegment"][];
            /**
             * Unit Preferences
             * @default []
             */
            unit_preferences: components["schemas"]["UnitPreference"][];
            waterbody_model: components["schemas"]["ModelSelection"];
        };
        /** AnalysisContextSummary */
        AnalysisContextSummary: {
            date_range: components["schemas"]["ContextSummaryDateRange"];
            location: components["schemas"]["ContextSummaryLocation"];
            mode: components["schemas"]["InferenceMode"];
            /** Params Used */
            params_used: components["schemas"]["ContextSummaryTargetParameter"][];
            /** Supporting Params */
            supporting_params: components["schemas"]["ContextSummaryParameter"][];
            /** Target Comid */
            target_comid: string;
            time_resolution: components["schemas"]["TimeResolution"];
        };
        /** AnalysisDateRangeV1 */
        AnalysisDateRangeV1: {
            /**
             * End Date
             * Format: date
             */
            end_date: string;
            /**
             * Start Date
             * Format: date
             */
            start_date: string;
        };
        /** AnalysisDetail */
        AnalysisDetail: {
            /** Steps */
            steps: components["schemas"]["AnalysisStep"][];
            /** Summary */
            summary: string;
            /**
             * Transcript Item Refs
             * @default []
             */
            transcript_item_refs: string[];
        };
        /**
         * AnalysisPhase
         * @enum {string}
         */
        AnalysisPhase: "queued" | "query_understanding" | "topology_building" | "topology_ready" | "waterbody_execution" | "run_summarization" | "terminal";
        /** AnalysisRunErrorResponse */
        AnalysisRunErrorResponse: {
            error: components["schemas"]["HttpPublicError"];
            /** History Url */
            history_url?: string | null;
            run?: components["schemas"]["RunStatusResponse"] | null;
            /**
             * Validation
             * @default []
             */
            validation: components["schemas"]["ValidationViolation"][];
        };
        /** AnalysisRunResponse */
        AnalysisRunResponse: {
            configuration: components["schemas"]["AnalysisConfigurationSnapshot"];
            /**
             * Created At
             * Format: date-time
             */
            created_at: string;
            /**
             * Run Id
             * Format: uuid
             */
            run_id: string;
            status: components["schemas"]["RunStatus"];
            /**
             * Updated At
             * Format: date-time
             */
            updated_at: string;
        };
        /** AnalysisStep */
        AnalysisStep: {
            content: components["schemas"]["JsonValue"];
            /**
             * Evidence Refs
             * @default []
             */
            evidence_refs: string[];
            /**
             * Result Cell Refs
             * @default []
             */
            result_cell_refs: string[];
            /** Title */
            title: string;
            /**
             * Transcript Item Refs
             * @default []
             */
            transcript_item_refs: string[];
            /** Type */
            type: string;
        };
        /** AnalysisTargetV1 */
        AnalysisTargetV1: {
            /** Comid */
            comid?: string;
            /** Coordinates */
            coordinates?: components["schemas"]["GeoPoint"];
        };
        /** AnalysisTimeSegment */
        AnalysisTimeSegment: {
            mode: components["schemas"]["InferenceMode"];
            /** Segment Index */
            segment_index: number;
            time_range: components["schemas"]["TimeRange"];
        };
        /** AnchorDistanceEstimate */
        AnchorDistanceEstimate: {
            /** Anchor Index */
            anchor_index: number;
            /** Cell Id */
            cell_id: string;
            /**
             * Distance Basis
             * @default mapped_channel_estimate
             * @constant
             */
            distance_basis: "mapped_channel_estimate";
            /** Distance Km */
            distance_km: number;
        };
        /** AnchorDistanceEstimatesResponse */
        AnchorDistanceEstimatesResponse: {
            /**
             * Estimates
             * @default []
             */
            estimates: components["schemas"]["AnchorDistanceEstimate"][];
            /**
             * Result Item Id
             * Format: uuid
             */
            result_item_id: string;
        };
        /**
         * AssessmentAvailability
         * @enum {string}
         */
        AssessmentAvailability: "complete" | "partial" | "indeterminate";
        /**
         * AttributionBasis
         * @enum {string}
         */
        AttributionBasis: "observed" | "correlated" | "inferred" | "hypothesized";
        /**
         * AttributionDirection
         * @enum {string}
         */
        AttributionDirection: "increasing" | "decreasing" | "neutral" | "mixed" | "unknown";
        /** AttributionFactor */
        AttributionFactor: {
            basis: components["schemas"]["AttributionBasis"];
            direction: components["schemas"]["AttributionDirection"];
            /** Evidence Refs */
            evidence_refs: string[];
            /** Explanation */
            explanation: string;
            /** Factor */
            factor: string;
            magnitude: components["schemas"]["AttributionMagnitude"];
            /**
             * Result Cell Refs
             * @default []
             */
            result_cell_refs: string[];
        };
        /**
         * AttributionMagnitude
         * @enum {string}
         */
        AttributionMagnitude: "low" | "medium" | "high" | "unknown";
        /** AttributionSummary */
        AttributionSummary: {
            /**
             * Primary Factors
             * @default []
             */
            primary_factors: components["schemas"]["AttributionFactor"][];
            /** Summary */
            summary: string;
        };
        /** BranchOutcome */
        BranchOutcome: {
            /**
             * Blocked Upstream Comid
             * @default null
             */
            blocked_upstream_comid: string | null;
            /** Branch Root Comid */
            branch_root_comid: string;
            coverage_unit: components["schemas"]["CoverageUnit"];
            outcome: components["schemas"]["BranchOutcomeStatus"];
            /** Path To Target */
            path_to_target: string[];
            reason: components["schemas"]["BranchOutcomeReason"];
            /** Stop Comid */
            stop_comid: string;
        };
        /**
         * BranchOutcomeReason
         * @enum {string}
         */
        BranchOutcomeReason: "target_station_qualified" | "station_qualified" | "natural_headwater" | "max_distance_reached" | "max_hops_reached" | "max_nodes_reached" | "multiple_limits_reached";
        /**
         * BranchOutcomeStatus
         * @enum {string}
         */
        BranchOutcomeStatus: "qualified" | "no_qualifying_source" | "search_limit_reached" | "backup_search_ended";
        /** ComidLayer */
        ComidLayer: string[];
        /** CompositeRiskAssessment */
        CompositeRiskAssessment: {
            /** Assessed Cell Count */
            assessed_cell_count: number;
            /** Assessment Id */
            assessment_id: string;
            availability: components["schemas"]["AssessmentAvailability"];
            /** Blocker Refs */
            blocker_refs: string[];
            /** Confidence */
            confidence: number;
            /** Confidence Rationale */
            confidence_rationale: string;
            /** Contributing Cell Refs */
            contributing_cell_refs: string[];
            /**
             * End Time
             * Format: date-time
             */
            end_time: string;
            /** Evidence Refs */
            evidence_refs: string[];
            /** Missing Cell Refs */
            missing_cell_refs: string[];
            risk_level: components["schemas"]["RiskLevel"];
            /** Risk Rationale */
            risk_rationale: string;
            /**
             * Start Time
             * Format: date-time
             */
            start_time: string;
            /** Total Cell Count */
            total_cell_count: number;
        };
        /** ConfirmActionRequest */
        ConfirmActionRequest: {
            /** Confirmation Phrase */
            confirmation_phrase: string;
            /** Confirmation Token */
            confirmation_token: string;
        };
        /** ContextSummaryDateRange */
        ContextSummaryDateRange: {
            /**
             * End Date
             * Format: date
             */
            end_date: string;
            /**
             * Start Date
             * Format: date
             */
            start_date: string;
        };
        /** ContextSummaryLocation */
        ContextSummaryLocation: {
            /** Display Name */
            display_name?: string | null;
            /** Latitude */
            latitude: number;
            /** Longitude */
            longitude: number;
        };
        /** ContextSummaryParameter */
        ContextSummaryParameter: {
            /** Canonical Name */
            canonical_name: string;
            /** Code */
            code: string;
            /** Code System */
            code_system: string;
        };
        /** ContextSummaryTargetParameter */
        ContextSummaryTargetParameter: {
            /** Canonical Name */
            canonical_name: string;
            /** Code */
            code: string;
            /** Code System */
            code_system: string;
            /** Rationale */
            rationale: string;
            source: components["schemas"]["ParameterIntentSource"];
        };
        /** CoverageUnit */
        CoverageUnit: {
            parameter: components["schemas"]["ParameterKey"];
            /** Segment Index */
            segment_index: number;
            /**
             * Window Index
             * @default null
             */
            window_index: number | null;
        };
        /** CreateAnalysisRunRequest */
        CreateAnalysisRunRequest: {
            /** Date Range */
            date_range?: components["schemas"]["AnalysisDateRangeV1"];
            memory_mode: components["schemas"]["MemoryMode"];
            parser_model: components["schemas"]["ModelSelection"];
            /** Query */
            query: string;
            /** Target */
            target?: components["schemas"]["AnalysisTargetV1"];
            /** @default 12h */
            time_resolution: components["schemas"]["TimeResolutionPreference"];
            waterbody_model: components["schemas"]["ModelSelection"];
        };
        /**
         * CredentialName
         * @enum {string}
         */
        CredentialName: "openai" | "google_geocoding";
        /**
         * CredentialSource
         * @enum {string}
         */
        CredentialSource: "user_store" | "deployment" | "none";
        /** CredentialTestResponse */
        CredentialTestResponse: {
            /**
             * Checked At
             * Format: date-time
             */
            checked_at: string;
            key_name: components["schemas"]["CredentialName"];
            safe_error: components["schemas"]["AdminSafeError"] | null;
            /**
             * Status
             * @enum {string}
             */
            status: "succeeded" | "failed";
        };
        /**
         * DataQualityFlag
         * @enum {string}
         */
        DataQualityFlag: "missing" | "partial_coverage" | "possible_freezing" | "anomalous" | "estimated" | "temporal_estimate" | "historical_inference" | "stale" | "provider_flagged" | "unit_unresolved" | "censored" | "counter_reset" | "invalid_protocol";
        /**
         * DependencyStatus
         * @enum {string}
         */
        DependencyStatus: "usable" | "insufficient" | "invalid" | "cancelled";
        /** EmbeddingProfileOption */
        EmbeddingProfileOption: {
            /** Available */
            available: boolean;
            /** Label */
            label: string;
            /** Profile Id */
            profile_id: string;
            /**
             * Unavailable Reason
             * @default null
             */
            unavailable_reason: string | null;
        };
        /** EvidenceChain */
        EvidenceChain: {
            /** References */
            references: components["schemas"]["EvidenceReference"][];
        };
        /**
         * EvidenceInferenceStrength
         * @enum {string}
         */
        EvidenceInferenceStrength: "directional";
        /** EvidenceReference */
        EvidenceReference: {
            /** Evidence Id */
            evidence_id: string;
            /** Role */
            role: string;
            /** Summary */
            summary: string | null;
        };
        /** EvidenceRule */
        EvidenceRule: {
            /** Input Parameters */
            input_parameters: components["schemas"]["ParameterKey"][];
            maximum_inference_strength: components["schemas"]["EvidenceInferenceStrength"];
            origin: components["schemas"]["EvidenceRuleOrigin"];
            /**
             * Origin Session Id
             * @default null
             */
            origin_session_id: string | null;
            output_parameter: components["schemas"]["ParameterKey"];
            /** Rationale */
            rationale: string;
            /** Required Collection Attempt */
            required_collection_attempt: boolean;
            /** Rule Id */
            rule_id: string;
            spatial_relation: components["schemas"]["EvidenceSpatialRelation"];
            temporal_relation: components["schemas"]["EvidenceTemporalRelation"];
        };
        /**
         * EvidenceRuleOrigin
         * @enum {string}
         */
        EvidenceRuleOrigin: "query_parser" | "waterbody_agent";
        /** EvidenceRulePlan */
        EvidenceRulePlan: {
            /**
             * Appended Rules
             * @default []
             */
            appended_rules: components["schemas"]["EvidenceRule"][];
            /**
             * Baseline Rules
             * @default []
             */
            baseline_rules: components["schemas"]["EvidenceRule"][];
            /** Plan Digest */
            plan_digest: string;
        };
        /**
         * EvidenceSpatialRelation
         * @enum {string}
         */
        EvidenceSpatialRelation: "same_waterbody" | "upstream_to_downstream";
        /**
         * EvidenceTemporalRelation
         * @enum {string}
         */
        EvidenceTemporalRelation: "same_window" | "overlapping_window" | "adjacent_local_day";
        /**
         * ExclusionReasonCode
         * @enum {string}
         */
        ExclusionReasonCode: "no_qualifying_source" | "search_limit_reached" | "backup_search_ended";
        /** GenerationDeletionResult */
        GenerationDeletionResult: {
            /** Deleted Generation Id */
            deleted_generation_id: string;
            /**
             * @description discriminator enum property added by openapi-typescript
             * @enum {string}
             */
            kind: "generation_deletion";
        };
        /** GenerationPreviewResponse */
        GenerationPreviewResponse: {
            /**
             * Action
             * @enum {string}
             */
            action: "retry_failed_jobs" | "generation_deletion";
            /** Affected Jobs */
            affected_jobs: number;
            confirmation: components["schemas"]["AdminConfirmation"] | null;
            /** Generation Id */
            generation_id: string;
        };
        /**
         * GenerationStatus
         * @enum {string}
         */
        GenerationStatus: "generating" | "completed" | "incomplete";
        /** GeoPoint */
        GeoPoint: {
            /** Latitude */
            latitude: number;
            /** Longitude */
            longitude: number;
        };
        /**
         * GraphCoverage
         * @enum {string}
         */
        GraphCoverage: "complete" | "partial" | "none";
        /** GraphOutcomeProjection */
        GraphOutcomeProjection: {
            /**
             * Cancelled Nodes
             * @default 0
             */
            cancelled_nodes: number;
            /** @default null */
            coverage: components["schemas"]["GraphCoverage"] | null;
            /**
             * Insufficient Nodes
             * @default 0
             */
            insufficient_nodes: number;
            /**
             * Invalid Nodes
             * @default 0
             */
            invalid_nodes: number;
            /**
             * Usable Nodes
             * @default 0
             */
            usable_nodes: number;
        };
        /** GuidelineConflictResponse */
        GuidelineConflictResponse: {
            current_document?: components["schemas"]["GuidelineDocumentDetails"] | null;
            error: components["schemas"]["HttpPublicError"];
            /** History Url */
            history_url?: string | null;
            run?: components["schemas"]["RunStatusResponse"] | null;
            /**
             * Validation
             * @default []
             */
            validation: components["schemas"]["ValidationViolation"][];
        };
        /** GuidelineContentV1 */
        GuidelineContentV1: {
            /** Body Markdown */
            body_markdown: string;
            /**
             * Keywords
             * @default []
             */
            keywords: string[];
            /**
             * Language
             * @enum {string}
             */
            language: "zh" | "en" | "mixed";
            /** Parameter Keys */
            parameter_keys: components["schemas"]["ParameterKey"][];
            /**
             * Related Parameter Keys
             * @default []
             */
            related_parameter_keys: components["schemas"]["ParameterKey"][];
            /**
             * Scenario Tags
             * @default []
             */
            scenario_tags: string[];
            /**
             * Schema Version
             * @default 1
             * @constant
             */
            schema_version: 1;
            source_kind: components["schemas"]["GuidelineSourceKind"];
            /**
             * Sources
             * @default []
             */
            sources: components["schemas"]["GuidelineSourceRef"][];
            /** Summary */
            summary: string;
            /** Title */
            title: string;
        };
        /** GuidelineDocumentDetails */
        GuidelineDocumentDetails: {
            /** Activation Epoch */
            activation_epoch: number;
            active_version?: components["schemas"]["GuidelineVersionV1"] | null;
            /** Active Version Id */
            active_version_id?: string | null;
            /**
             * Disabled
             * @default false
             */
            disabled: boolean;
            draft?: components["schemas"]["GuidelineDraftV1"] | null;
            /** Draft Digest */
            draft_digest?: string | null;
            /** Effective Version Id */
            effective_version_id?: string | null;
            /**
             * Has Bundled Update
             * @default false
             */
            has_bundled_update: boolean;
            /**
             * Has Unpublished Changes
             * @default false
             */
            has_unpublished_changes: boolean;
            identity: components["schemas"]["GuidelineIdentity"];
            latest_bundled_version?: components["schemas"]["GuidelineVersionV1"] | null;
            /**
             * Latest Version Number
             * @default 0
             */
            latest_version_number: number;
            pending_version?: components["schemas"]["GuidelineVersionV1"] | null;
            /** Pending Version Id */
            pending_version_id?: string | null;
            preparation?: components["schemas"]["GuidelineOperation"] | null;
            /** Revision */
            revision: number;
            /**
             * State
             * @enum {string}
             */
            state: "draft" | "preparing" | "available" | "preparation_failed" | "disabled" | "unavailable";
        };
        /** GuidelineDraftSourceRef */
        GuidelineDraftSourceRef: {
            /** Accessed Date */
            accessed_date?: string | null;
            /** Locator */
            locator?: string | null;
            /** Published Date */
            published_date?: string | null;
            /** Source Id */
            source_id: string;
            /** Title */
            title?: string | null;
            /** Url */
            url?: string | null;
        };
        /** GuidelineDraftV1 */
        GuidelineDraftV1: {
            /** Body Markdown */
            body_markdown?: string | null;
            /**
             * Keywords
             * @default []
             */
            keywords: string[];
            /** Language */
            language?: ("zh" | "en" | "mixed") | null;
            /**
             * Parameter Keys
             * @default []
             */
            parameter_keys: components["schemas"]["ParameterKey"][];
            /**
             * Related Parameter Keys
             * @default []
             */
            related_parameter_keys: components["schemas"]["ParameterKey"][];
            /**
             * Scenario Tags
             * @default []
             */
            scenario_tags: string[];
            /**
             * Schema Version
             * @default 1
             * @constant
             */
            schema_version: 1;
            source_kind?: components["schemas"]["GuidelineSourceKind"] | null;
            /**
             * Sources
             * @default []
             */
            sources: components["schemas"]["GuidelineDraftSourceRef"][];
            /** Summary */
            summary?: string | null;
            /** Title */
            title?: string | null;
        };
        /** GuidelineIdentity */
        GuidelineIdentity: {
            /** Base Bundle Key */
            base_bundle_key?: string | null;
            /** Base Content Digest */
            base_content_digest?: string | null;
            /** Based On Version Id */
            based_on_version_id?: string | null;
            /** Bundle Key */
            bundle_key?: string | null;
            /**
             * Family Root Id
             * Format: uuid
             */
            family_root_id: string;
            /**
             * Guideline Id
             * Format: uuid
             */
            guideline_id: string;
            origin: components["schemas"]["GuidelineOrigin"];
        };
        /** GuidelineListItem */
        GuidelineListItem: {
            /** Activation Epoch */
            activation_epoch: number;
            /** Active Version Id */
            active_version_id: string | null;
            /** Draft Digest */
            draft_digest: string | null;
            /** Effective Version Id */
            effective_version_id: string | null;
            /** Has Bundled Update */
            has_bundled_update: boolean;
            /** Has Unpublished Changes */
            has_unpublished_changes: boolean;
            identity: components["schemas"]["GuidelineIdentity"];
            /** Latest Bundled Version Id */
            latest_bundled_version_id: string | null;
            /** Pending Version Id */
            pending_version_id: string | null;
            /** Revision */
            revision: number;
            source_kind: components["schemas"]["GuidelineSourceKind"] | null;
            /**
             * State
             * @enum {string}
             */
            state: "draft" | "preparing" | "available" | "preparation_failed" | "disabled" | "unavailable";
            /** Summary */
            summary: string | null;
            /** Title */
            title: string | null;
        };
        /** GuidelineListResponse */
        GuidelineListResponse: {
            /** Items */
            items: components["schemas"]["GuidelineListItem"][];
            /** Next Cursor */
            next_cursor: string | null;
        };
        /** GuidelineModelViewV1 */
        GuidelineModelViewV1: {
            /** Body */
            body: string;
            /** Content Digest */
            content_digest: string;
            /**
             * Schema Version
             * @default 1
             * @constant
             */
            schema_version: 1;
            /** Source Ids */
            source_ids: string[];
            source_kind: components["schemas"]["GuidelineSourceKind"];
            /** Summary */
            summary: string;
            /** Title */
            title: string;
        };
        /** GuidelineMutation */
        GuidelineMutation: {
            /**
             * Idempotency Key
             * Format: uuid4
             */
            idempotency_key: string;
            kind: components["schemas"]["GuidelineOperationKind"];
            /** Request Fingerprint */
            request_fingerprint: string;
        };
        /** GuidelineMutationReceipt */
        GuidelineMutationReceipt: {
            /** Accepted Revision */
            accepted_revision?: number | null;
            /** Content Digest */
            content_digest?: string | null;
            /** Document Id */
            document_id?: string | null;
            mutation: components["schemas"]["GuidelineMutation"];
            /**
             * Operation Id
             * Format: uuid
             */
            operation_id: string;
            /**
             * Status Code
             * @enum {integer}
             */
            status_code: 200 | 201 | 202;
            /** Version Id */
            version_id?: string | null;
        };
        /** GuidelineOperation */
        GuidelineOperation: {
            /** Activation Epoch */
            activation_epoch?: number | null;
            /**
             * Attempts
             * @default 0
             */
            attempts: number;
            /** Generation Id */
            generation_id?: string | null;
            /** Lease Owner */
            lease_owner?: string | null;
            /** Lease Until */
            lease_until?: string | null;
            receipt: components["schemas"]["GuidelineMutationReceipt"];
            /** Safe Error Code */
            safe_error_code?: string | null;
            state: components["schemas"]["GuidelineOperationState"];
        };
        /**
         * GuidelineOperationKind
         * @enum {string}
         */
        GuidelineOperationKind: "create" | "save_draft" | "local_edit" | "publish" | "set_enabled" | "restore_bundled" | "rebuild_index" | "retry" | "import_bundled";
        /**
         * GuidelineOperationState
         * @enum {string}
         */
        GuidelineOperationState: "pending" | "running" | "succeeded" | "failed" | "superseded";
        /**
         * GuidelineOrigin
         * @enum {string}
         */
        GuidelineOrigin: "bundled" | "local_custom" | "local_override";
        /** GuidelineReview */
        GuidelineReview: {
            /** Content Digest */
            content_digest: string;
            /**
             * Guideline Id
             * Format: uuid
             */
            guideline_id: string;
            /**
             * Review Confirmed
             * @constant
             */
            review_confirmed: true;
            /** Review Note */
            review_note: string;
            /**
             * Reviewed At
             * Format: date-time
             */
            reviewed_at: string;
            /** Reviewer */
            reviewer: string;
        };
        /**
         * GuidelineSourceKind
         * @enum {string}
         */
        GuidelineSourceKind: "reference_derived" | "user_authored";
        /** GuidelineSourceRef */
        GuidelineSourceRef: {
            /** Accessed Date */
            accessed_date?: string | null;
            /** Locator */
            locator?: string | null;
            /** Published Date */
            published_date?: string | null;
            /** Source Id */
            source_id: string;
            /** Title */
            title: string;
            /** Url */
            url?: string | null;
        };
        /** GuidelineVersionV1 */
        GuidelineVersionV1: {
            content: components["schemas"]["GuidelineContentV1"];
            identity: components["schemas"]["GuidelineIdentity"];
            model_view: components["schemas"]["GuidelineModelViewV1"];
            review: components["schemas"]["GuidelineReview"];
            /**
             * Schema Version
             * @default 1
             * @constant
             */
            schema_version: 1;
            /**
             * Version Id
             * Format: uuid
             */
            version_id: string;
            /** Version Number */
            version_number: number;
        };
        /** HistoryCleanupEstimate */
        HistoryCleanupEstimate: {
            /** Retained Idempotency Tombstones */
            retained_idempotency_tombstones: number;
            /** Runs */
            runs: number;
            /** Sessions */
            sessions: number;
            /** Transcript Items */
            transcript_items: number;
        };
        /** HistoryCleanupPreviewRequest */
        HistoryCleanupPreviewRequest: {
            /** Cascade Consumers */
            cascade_consumers: boolean;
            /** Selection */
            selection: components["schemas"]["RunSelection"] | components["schemas"]["AllHistorySelection"];
        };
        /** HistoryCleanupPreviewResponse */
        HistoryCleanupPreviewResponse: {
            /** Cascade Authorized */
            cascade_authorized: boolean;
            /** Cascade Required */
            cascade_required: boolean;
            confirmation: components["schemas"]["AdminConfirmation"] | null;
            /** Delete Order */
            delete_order: string[];
            /** Delete Run Ids */
            delete_run_ids: string[];
            /** Dependencies */
            dependencies: string[];
            estimated: components["schemas"]["HistoryCleanupEstimate"];
            /** Preserved */
            preserved: ("api_keys" | "settings" | "unit_policy_versions")[];
            /** Requested Run Ids */
            requested_run_ids: string[];
        };
        /** HistoryCleanupResult */
        HistoryCleanupResult: {
            /** Deleted Run Ids */
            deleted_run_ids: string[];
            /**
             * @description discriminator enum property added by openapi-typescript
             * @enum {string}
             */
            kind: "history_cleanup";
            /** Retained Idempotency Tombstones */
            retained_idempotency_tombstones: number;
        };
        /** HttpPublicError */
        HttpPublicError: {
            /** Code */
            code: string;
            /**
             * Error Id
             * Format: uuid
             */
            error_id: string;
            /**
             * Failed Stage
             * @default null
             */
            failed_stage: string | null;
            /** Message */
            message: string;
            /**
             * Request Id
             * Format: uuid
             */
            request_id: string;
            /** Retryable */
            retryable: boolean;
            /**
             * Run Id
             * @default null
             */
            run_id: string | null;
            /**
             * Session Id
             * @default null
             */
            session_id: string | null;
        };
        /** HydrologyEdge */
        HydrologyEdge: {
            /** Distance Km */
            distance_km: number;
            /** Downstream Comid */
            downstream_comid: string;
            /** Participating */
            participating: boolean;
            /** Upstream Comid */
            upstream_comid: string;
        };
        /** HydrologyExclusionReason */
        HydrologyExclusionReason: {
            /**
             * Blocked Upstream Comid
             * @default null
             */
            blocked_upstream_comid: string | null;
            /** Branch Root Comid */
            branch_root_comid: string;
            code: components["schemas"]["ExclusionReasonCode"];
            coverage_unit: components["schemas"]["CoverageUnit"];
            /** Stop Comid */
            stop_comid: string;
        };
        /** HydrologyGraph */
        HydrologyGraph: {
            /** Branch Outcomes */
            branch_outcomes: components["schemas"]["BranchOutcome"][];
            coverage: components["schemas"]["TopologyCoverage"];
            /** Edges */
            edges: components["schemas"]["HydrologyEdge"][];
            /** Execution Layers */
            execution_layers: components["schemas"]["ComidLayer"][];
            /** Geometries */
            geometries?: {
                [key: string]: components["schemas"]["WaterbodyGeometry"];
            };
            limits: components["schemas"]["TopologyLimits"];
            /** Nodes */
            nodes: components["schemas"]["HydrologyNode"][];
            /**
             * Schema Version
             * @default 1
             * @constant
             */
            schema_version: 1;
            stats: components["schemas"]["TopologySearchStats"];
            /** Target Comid */
            target_comid: string;
        };
        /** HydrologyNode */
        HydrologyNode: {
            /** Comid */
            comid: string;
            /** Cumulative Distance Km */
            cumulative_distance_km: number;
            /** Depth */
            depth: number;
            /**
             * Excluded Reasons
             * @default []
             */
            excluded_reasons: components["schemas"]["HydrologyExclusionReason"][];
            /**
             * Geometry Ref
             * @default null
             */
            geometry_ref: string | null;
            /** @default null */
            geometry_summary: components["schemas"]["WaterbodyGeometrySummary"] | null;
            /** Is Inlet */
            is_inlet: boolean;
            /** Is Target */
            is_target: boolean;
            participation: components["schemas"]["HydrologyParticipation"];
            point: components["schemas"]["GeoPoint"] | null;
            /**
             * Qualified Units
             * @default []
             */
            qualified_units: components["schemas"]["CoverageUnit"][];
            /**
             * Station Refs
             * @default []
             */
            station_refs: components["schemas"]["TopologyStationRef"][];
        };
        /**
         * HydrologyParticipation
         * @enum {string}
         */
        HydrologyParticipation: "active" | "excluded";
        /**
         * IndeterminateReason
         * @enum {string}
         */
        IndeterminateReason: "insufficient_evidence" | "no_station_path" | "parameter_unobserved" | "invalid_observations" | "source_unavailable" | "no_applicable_evidence";
        /**
         * InferenceMode
         * @enum {string}
         */
        InferenceMode: "gap_filling" | "prediction" | "mixed";
        /** InferenceResult */
        InferenceResult: {
            analysis_detail: components["schemas"]["AnalysisDetail"];
            /** Analysis Timezone */
            analysis_timezone: string;
            attribution_summary: components["schemas"]["AttributionSummary"];
            evidence_chain: components["schemas"]["EvidenceChain"];
            /**
             * Generated At
             * Format: date-time
             */
            generated_at: string;
            mode: components["schemas"]["InferenceMode"];
            model_execution_summary: components["schemas"]["ModelExecutionSummary"];
            overall_assessment: components["schemas"]["OverallWaterbodyAssessment"];
            /** Overall Confidence */
            overall_confidence: number;
            /** Overall Confidence Rationale */
            overall_confidence_rationale: string;
            /** Requested Parameters */
            requested_parameters: components["schemas"]["ParameterKey"][];
            /** Result Id */
            result_id: string;
            /**
             * Run Id
             * Format: uuid
             */
            run_id: string;
            /** Series */
            series: components["schemas"]["ParameterTimeSeries"][];
            /**
             * Session Id
             * Format: uuid
             */
            session_id: string;
            /** Target Comid */
            target_comid: string;
            time_range: components["schemas"]["TimeRange"];
            /** @default 12h */
            time_resolution: components["schemas"]["TimeResolution"];
            time_resolution_source: components["schemas"]["TimeResolutionSource"];
            /** Time Segments */
            time_segments: components["schemas"]["TimeSegment"][];
            /**
             * Unit Decisions
             * @default []
             */
            unit_decisions: components["schemas"]["UnitPreference"][];
            /** Window Assessments */
            window_assessments: components["schemas"]["CompositeRiskAssessment"][];
        };
        /** InlineTranscriptContent */
        InlineTranscriptContent: {
            body: components["schemas"]["JsonValue"];
            /**
             * @description discriminator enum property added by openapi-typescript
             * @enum {string}
             */
            kind: "inline";
        };
        JsonValue: unknown;
        /** KeyStatusItem */
        KeyStatusItem: {
            /** Can Delete User Override */
            can_delete_user_override: boolean;
            /** Can Save */
            can_save: boolean;
            /** Can Test */
            can_test: boolean;
            /** Configured */
            configured: boolean;
            key_name: components["schemas"]["CredentialName"];
            /** Label */
            label: string;
            /** Masked */
            masked: string | null;
            /** Purpose */
            purpose: string[];
            source: components["schemas"]["CredentialSource"];
            /** Store Available */
            store_available: boolean;
            /** Updated At */
            updated_at: string | null;
        };
        /** KeyStatusResponse */
        KeyStatusResponse: {
            /** Keys */
            keys: components["schemas"]["KeyStatusItem"][];
        };
        /** LocalDateRange */
        LocalDateRange: {
            /**
             * End Date
             * Format: date
             */
            end_date: string;
            /**
             * Start Date
             * Format: date
             */
            start_date: string;
        };
        /** LocalEditGuidelineRequest */
        LocalEditGuidelineRequest: {
            /** Base Content Digest */
            base_content_digest: string;
            /**
             * Base Version Id
             * Format: uuid
             */
            base_version_id: string;
            /** Expected Revision */
            expected_revision: number;
        };
        /** MeasuredAnchor */
        MeasuredAnchor: {
            /**
             * Distance Basis
             * @default null
             */
            distance_basis: "mapped_channel_estimate" | null;
            /**
             * Distance Km
             * @default null
             */
            distance_km: number | null;
            evidence_time_range: components["schemas"]["TimeRange"];
            /** Origin Comid */
            origin_comid: string;
            /** @default null */
            origin_point: components["schemas"]["GeoPoint"] | null;
            parameter: components["schemas"]["ParameterKey"];
            /** Path Comids */
            path_comids: string[];
            /** Source Record Refs */
            source_record_refs: components["schemas"]["SourceRecordReference"][];
            /**
             * Station Id
             * @default null
             */
            station_id: string | null;
        };
        /**
         * MemoryMode
         * @enum {string}
         */
        MemoryMode: "stateless" | "memory_augmented";
        /** MessageDeltaPayload */
        MessageDeltaPayload: {
            /**
             * Item Id
             * Format: uuid
             */
            item_id: string;
            /** Text */
            text: string;
        };
        /** MessageFinalizedPayload */
        MessageFinalizedPayload: {
            generation_status: components["schemas"]["GenerationStatus"];
            item: components["schemas"]["TranscriptItemView"];
        };
        /** MessageStartedPayload */
        MessageStartedPayload: {
            item: components["schemas"]["TranscriptItemHeader"];
        };
        /** ModelExecutionSummary */
        ModelExecutionSummary: {
            model: components["schemas"]["ModelSelection"];
            /**
             * Numeric Estimation Calls
             * @default 0
             */
            numeric_estimation_calls: number;
            /**
             * Numeric Failure Codes
             * @default []
             */
            numeric_failure_codes: string[];
            /** Planning Calls */
            planning_calls: number;
            /** Reasoning Batches */
            reasoning_batches: number;
            /** Repair Calls */
            repair_calls: number;
            /** Skipped Cells */
            skipped_cells: number;
            /** Synthesis Calls */
            synthesis_calls: number;
            /** Tool Calls */
            tool_calls: number;
            /** @default null */
            usage: components["schemas"]["ModelUsageSummary"] | null;
        };
        /** ModelOption */
        ModelOption: {
            /** Available */
            available: boolean;
            /** Label */
            label: string;
            /** Roles */
            roles: components["schemas"]["ModelRole"][];
            selection: components["schemas"]["ModelSelection"];
            /**
             * Unavailable Reason
             * @default null
             */
            unavailable_reason: string | null;
        };
        /**
         * ModelRole
         * @enum {string}
         */
        ModelRole: "query_parser" | "waterbody" | "embedding";
        /** ModelSelection */
        ModelSelection: {
            /** Model */
            model: string;
            /** Provider */
            provider: string;
        };
        /** ModelUsageSummary */
        ModelUsageSummary: {
            /**
             * Input Tokens
             * @default null
             */
            input_tokens: number | null;
            /**
             * Output Tokens
             * @default null
             */
            output_tokens: number | null;
        };
        /** MutableDefaults */
        MutableDefaults: {
            /** Analysis Timezone */
            analysis_timezone: string;
            /** Desired Embedding Profile Id */
            desired_embedding_profile_id: string;
            memory_mode: components["schemas"]["MemoryMode"];
            parser_model: components["schemas"]["ModelSelection"];
            time_resolution: components["schemas"]["TimeResolutionPreference"];
            waterbody_model: components["schemas"]["ModelSelection"];
        };
        /** OutcomeCounts */
        OutcomeCounts: {
            /**
             * Cancelled
             * @default 0
             */
            cancelled: number;
            /**
             * Insufficient
             * @default 0
             */
            insufficient: number;
            /**
             * Invalid
             * @default 0
             */
            invalid: number;
            /**
             * Usable
             * @default 0
             */
            usable: number;
        };
        /**
         * OutcomeStatus
         * @enum {string}
         */
        OutcomeStatus: "quantified" | "range_estimate" | "directional" | "indeterminate";
        /** OverallWaterbodyAssessment */
        OverallWaterbodyAssessment: {
            /** Assessed Cell Count */
            assessed_cell_count: number;
            /** Assessment Id */
            assessment_id: string;
            availability: components["schemas"]["AssessmentAvailability"];
            /** Blocker Refs */
            blocker_refs: string[];
            /** Confidence */
            confidence: number;
            /** Confidence Rationale */
            confidence_rationale: string;
            /** Contributing Cell Refs */
            contributing_cell_refs: string[];
            /** Contributing Window Refs */
            contributing_window_refs: string[];
            /** Evidence Refs */
            evidence_refs: string[];
            /** Missing Cell Refs */
            missing_cell_refs: string[];
            risk_level: components["schemas"]["RiskLevel"];
            /** Risk Rationale */
            risk_rationale: string;
            time_range: components["schemas"]["TimeRange"];
            /** Total Cell Count */
            total_cell_count: number;
        };
        /**
         * ParameterIntentSource
         * @enum {string}
         */
        ParameterIntentSource: "explicit" | "inferred";
        /** ParameterKey */
        ParameterKey: {
            /** Code */
            code: string;
            /** Code System */
            code_system: string;
        };
        /**
         * ParameterProvider
         * @enum {string}
         */
        ParameterProvider: "wdfn_time_series" | "usgs_samples";
        /** ParameterTimeSeries */
        ParameterTimeSeries: {
            /** Confidence */
            confidence: number;
            /** Confidence Rationale */
            confidence_rationale: string;
            /** Contributing Cell Refs */
            contributing_cell_refs: string[];
            parameter: components["schemas"]["WaterQualityParameter"];
            /** Points */
            points: components["schemas"]["TimeWindowResult"][];
            /** @default 12h */
            time_resolution: components["schemas"]["TimeResolution"];
            time_resolution_source: components["schemas"]["TimeResolutionSource"];
        };
        /**
         * ParseStatus
         * @enum {string}
         */
        ParseStatus: "ok" | "unsupported" | "corrupt";
        /** PersistenceInfo */
        PersistenceInfo: {
            state: components["schemas"]["PersistenceState"];
            /** @default null */
            warning: components["schemas"]["PublicError"] | null;
        };
        /** PersistenceRetryResponse */
        PersistenceRetryResponse: {
            persistence: components["schemas"]["PersistenceInfo"];
            /**
             * Run Id
             * Format: uuid
             */
            run_id: string;
        };
        /**
         * PersistenceState
         * @enum {string}
         */
        PersistenceState: "up_to_date" | "pending" | "retry_exhausted";
        /** PreviewUnitPolicyRequest */
        PreviewUnitPolicyRequest: {
            /** Base Version */
            base_version: number;
            /** Entries */
            entries: components["schemas"]["UnitPolicyEntry"][];
        };
        /** ProfileHandoffPreviewResponse */
        ProfileHandoffPreviewResponse: {
            /**
             * Action
             * @default profile_handoff
             * @constant
             */
            action: "profile_handoff";
            confirmation: components["schemas"]["AdminConfirmation"] | null;
            /** Estimated Jobs */
            estimated_jobs: number;
            handoff: components["schemas"]["RAGProfileHandoffState"];
            /** Provider Usage Warning */
            provider_usage_warning: boolean;
        };
        /** ProfileHandoffResult */
        ProfileHandoffResult: {
            /** Active Generation Id */
            active_generation_id: string;
            /**
             * @description discriminator enum property added by openapi-typescript
             * @enum {string}
             */
            kind: "profile_handoff";
        };
        /** PublicError */
        PublicError: {
            /** Code */
            code: string;
            /**
             * Error Id
             * Format: uuid
             */
            error_id: string;
            /**
             * Failed Stage
             * @default null
             */
            failed_stage: string | null;
            /** Message */
            message: string;
            /** Retryable */
            retryable: boolean;
        };
        /** PublishGuidelineRequest */
        PublishGuidelineRequest: {
            /** Content Digest */
            content_digest: string;
            /** Expected Revision */
            expected_revision: number;
            /** Review Confirmed */
            review_confirmed: boolean;
            /** Review Note */
            review_note: string;
            /** Reviewer */
            reviewer: string;
        };
        /** PutCredentialRequest */
        PutCredentialRequest: {
            /**
             * Value
             * Format: password
             */
            value: string;
        };
        /** RAGGenerationHealth */
        RAGGenerationHealth: {
            /**
             * Activated At
             * @default null
             */
            activated_at: string | null;
            /**
             * Created At
             * Format: date-time
             */
            created_at: string;
            /**
             * Failed At
             * @default null
             */
            failed_at: string | null;
            /** Generation Id */
            generation_id: string;
            job_counts: components["schemas"]["RAGJobCounts"];
            /**
             * Last Safe Error Code
             * @default null
             */
            last_safe_error_code: string | null;
            /** Profile Digest */
            profile_digest: string;
            /** Reader Count */
            reader_count: number;
            /**
             * Retired At
             * @default null
             */
            retired_at: string | null;
            status: components["schemas"]["RAGIndexGenerationStatus"];
        };
        /**
         * RAGIndexGenerationStatus
         * @enum {string}
         */
        RAGIndexGenerationStatus: "building" | "active" | "retired" | "failed";
        /** RAGIndexHealth */
        RAGIndexHealth: {
            /**
             * Active Generation Id
             * @default null
             */
            active_generation_id: string | null;
            /** Generations */
            generations: components["schemas"]["RAGGenerationHealth"][];
        };
        /** RAGJobCounts */
        RAGJobCounts: {
            /**
             * Completed
             * @default 0
             */
            completed: number;
            /**
             * Excluded
             * @default 0
             */
            excluded: number;
            /**
             * Failed
             * @default 0
             */
            failed: number;
            /**
             * Leased
             * @default 0
             */
            leased: number;
            /**
             * Pending
             * @default 0
             */
            pending: number;
            /**
             * Retry Wait
             * @default 0
             */
            retry_wait: number;
        };
        /**
         * RAGProfileHandoffPhase
         * @enum {string}
         */
        RAGProfileHandoffPhase: "build_required" | "building" | "reader_drain" | "credential_cleanup_ready";
        /** RAGProfileHandoffState */
        RAGProfileHandoffState: {
            /**
             * Active Generation Id
             * @default null
             */
            active_generation_id: string | null;
            /**
             * Blocking Reader Count
             * @default 0
             */
            blocking_reader_count: number;
            /**
             * Building Generation Id
             * @default null
             */
            building_generation_id: string | null;
            /** Desired Profile Digest */
            desired_profile_digest: string;
            /**
             * Old Credential Cleanup Ready
             * @default false
             */
            old_credential_cleanup_ready: boolean;
            phase: components["schemas"]["RAGProfileHandoffPhase"];
            /**
             * Retired Generation Ids
             * @default []
             */
            retired_generation_ids: string[];
        };
        /** ReadIssue */
        ReadIssue: {
            error: components["schemas"]["PublicError"];
            /**
             * Item Id
             * Format: uuid
             */
            item_id: string;
        };
        /** ReadSessionSummary */
        ReadSessionSummary: {
            agent_role: components["schemas"]["AgentRole"];
            /**
             * Created At
             * Format: date-time
             */
            created_at: string;
            result_ref?: components["schemas"]["SessionResultReference"] | null;
            /** Result Summary */
            result_summary?: (components["schemas"]["WaterbodySessionResultSummary"] | components["schemas"]["RunSummarySessionResultSummary"]) | null;
            /**
             * Session Id
             * Format: uuid
             */
            session_id: string;
            status: components["schemas"]["AgentSessionStatus"];
            /** Target Comid */
            target_comid?: string | null;
            /**
             * Terminal Corrupt
             * @default false
             */
            terminal_corrupt: boolean;
            /**
             * Updated At
             * Format: date-time
             */
            updated_at: string;
        };
        /**
         * ReadSource
         * @enum {string}
         */
        ReadSource: "live" | "postgresql";
        /**
         * ReadView
         * @enum {string}
         */
        ReadView: "auto" | "history";
        /** ReferencedTranscriptContent */
        ReferencedTranscriptContent: {
            /**
             * @description discriminator enum property added by openapi-typescript
             * @enum {string}
             */
            kind: "reference";
            /** Preview Head */
            preview_head: string;
            /** Preview Tail */
            preview_tail: string;
            /**
             * Truncated
             * @default true
             * @constant
             */
            truncated: true;
        };
        /** ReplaceSettingsRequest */
        ReplaceSettingsRequest: {
            defaults: components["schemas"]["MutableDefaults"];
            /** Expected Revision */
            expected_revision: number;
        };
        /** ResolvedParameterIntent */
        ResolvedParameterIntent: {
            /** Canonical Name */
            canonical_name: string;
            /** Canonical Unit */
            canonical_unit: string;
            key: components["schemas"]["ParameterKey"];
            /** Rationale */
            rationale: string;
            source: components["schemas"]["ParameterIntentSource"];
        };
        /** ResolvedTarget */
        ResolvedTarget: {
            /** Comid */
            comid: string;
            /**
             * Country Code
             * @constant
             */
            country_code: "US";
            /**
             * Display Name
             * @default null
             */
            display_name: string | null;
            point: components["schemas"]["GeoPoint"];
            source: components["schemas"]["TargetSource"];
            /** Subdivision Code */
            subdivision_code: string;
        };
        /** RestoreBundledGuidelineRequest */
        RestoreBundledGuidelineRequest: {
            /** Expected Revision */
            expected_revision: number;
            /**
             * Version Id
             * Format: uuid
             */
            version_id: string;
        };
        /**
         * ResultSource
         * @enum {string}
         */
        ResultSource: "observed" | "inferred" | "predicted";
        /** RetryFailedJobsPreviewRequest */
        RetryFailedJobsPreviewRequest: {
            /** Limit */
            limit: number;
            /** Safe Error Codes */
            safe_error_codes: string[];
        };
        /** RetryFailedJobsResult */
        RetryFailedJobsResult: {
            /**
             * @description discriminator enum property added by openapi-typescript
             * @enum {string}
             */
            kind: "retry_failed_jobs";
            /** Retried Jobs */
            retried_jobs: number;
        };
        /** RetryGuidelineOperationRequest */
        RetryGuidelineOperationRequest: {
            /** Expected Revision */
            expected_revision: number;
        };
        /**
         * RiskLevel
         * @enum {string}
         */
        RiskLevel: "none" | "low" | "medium" | "high" | "indeterminate";
        /** RiskOrTrendAssessment */
        RiskOrTrendAssessment: {
            /**
             * Reason Codes
             * @default []
             */
            reason_codes: components["schemas"]["IndeterminateReason"][];
            risk_level: components["schemas"]["RiskLevel"];
            /** Risk Rationale */
            risk_rationale: string;
            /** Summary */
            summary: string;
            /** Supporting Refs */
            supporting_refs: string[];
            trend: components["schemas"]["TrendDirection"] | null;
        };
        /** RunDetailResponse */
        RunDetailResponse: {
            /**
             * Issues
             * @default []
             */
            issues: components["schemas"]["ReadIssue"][];
            /**
             * Items
             * @default []
             */
            items: components["schemas"]["TranscriptItemView"][];
            persistence?: components["schemas"]["PersistenceInfo"] | null;
            /**
             * Referenced Items
             * @default []
             */
            referenced_items: components["schemas"]["TranscriptItemView"][];
            run: components["schemas"]["RunSummary"];
            /**
             * Sessions
             * @default []
             */
            sessions: (components["schemas"]["SessionSummary"] | components["schemas"]["ReadSessionSummary"])[];
            source: components["schemas"]["ReadSource"];
        };
        /** RunHistoryItem */
        RunHistoryItem: {
            configuration?: components["schemas"]["AnalysisConfigurationSnapshot"] | null;
            context_summary: components["schemas"]["AnalysisContextSummary"] | null;
            /**
             * Created At
             * Format: date-time
             */
            created_at: string;
            /**
             * Issues
             * @default []
             */
            issues: components["schemas"]["ReadIssue"][];
            /** Query */
            query: string;
            result_ref: components["schemas"]["TranscriptContentRef"] | null;
            result_summary: components["schemas"]["TopologyRunResultSummary"] | null;
            /**
             * Run Id
             * Format: uuid
             */
            run_id: string;
            status: components["schemas"]["RunStatus"];
            /**
             * Updated At
             * Format: date-time
             */
            updated_at: string;
        };
        /** RunHistoryResponse */
        RunHistoryResponse: {
            /**
             * Items
             * @default []
             */
            items: components["schemas"]["RunHistoryItem"][];
            /** Next Cursor */
            next_cursor?: string | null;
        };
        /** RunSelection */
        RunSelection: {
            /**
             * @description discriminator enum property added by openapi-typescript
             * @enum {string}
             */
            kind: "runs";
            /** Run Ids */
            run_ids: string[];
        };
        /** RunSnapshotEvent */
        RunSnapshotEvent: {
            analysis_phase: components["schemas"]["AnalysisPhase"];
            context_ref: components["schemas"]["TranscriptContentRef"] | null;
            /**
             * Created At
             * Format: date-time
             */
            created_at: string;
            graph_outcome: components["schemas"]["GraphOutcomeProjection"];
            persistence: components["schemas"]["PersistenceInfo"];
            result_ref: components["schemas"]["TranscriptContentRef"] | null;
            /**
             * Run Id
             * Format: uuid
             */
            run_id: string;
            /**
             * Schema Version
             * @default 1
             * @constant
             */
            schema_version: 1;
            /**
             * Sessions
             * @default []
             */
            sessions: components["schemas"]["SessionSummary"][];
            status: components["schemas"]["RunStatus"];
            summary: components["schemas"]["RunSummaryProjection"];
            topology_ref: components["schemas"]["TranscriptContentRef"] | null;
            /**
             * Updated At
             * Format: date-time
             */
            updated_at: string;
            /**
             * Waterbodies
             * @default []
             */
            waterbodies: components["schemas"]["RunWaterbodySummary"][];
        };
        /**
         * RunStatus
         * @enum {string}
         */
        RunStatus: "pending" | "running" | "completed" | "failed" | "cancelled" | "completed_with_persistence_error";
        /** RunStatusResponse */
        RunStatusResponse: {
            /**
             * Created At
             * Format: date-time
             */
            created_at: string;
            /** @default null */
            persistence: components["schemas"]["PersistenceInfo"] | null;
            /**
             * Run Id
             * Format: uuid
             */
            run_id: string;
            status: components["schemas"]["RunStatus"];
            /**
             * Updated At
             * Format: date-time
             */
            updated_at: string;
        };
        /** RunSummary */
        RunSummary: {
            configuration?: components["schemas"]["AnalysisConfigurationSnapshot"] | null;
            context_summary: components["schemas"]["AnalysisContextSummary"] | null;
            /**
             * Created At
             * Format: date-time
             */
            created_at: string;
            /** Query */
            query: string;
            result_ref: components["schemas"]["TranscriptContentRef"] | null;
            result_summary: components["schemas"]["TopologyRunResultSummary"] | null;
            /**
             * Run Id
             * Format: uuid
             */
            run_id: string;
            status: components["schemas"]["RunStatus"];
            /**
             * Updated At
             * Format: date-time
             */
            updated_at: string;
        };
        /** RunSummaryHighlight */
        RunSummaryHighlight: {
            /**
             * Display Unit
             * @default null
             */
            display_unit: string | null;
            /**
             * Display Value
             * @default null
             */
            display_value: string | null;
            /** Kind */
            kind: string;
            source_ref: components["schemas"]["StableSourceReference"];
            /** Text */
            text: string;
            /**
             * Value Lower
             * @default null
             */
            value_lower: string | null;
            /**
             * Value Upper
             * @default null
             */
            value_upper: string | null;
            /**
             * Window Id
             * @default null
             */
            window_id: string | null;
        };
        /** RunSummaryProjection */
        RunSummaryProjection: {
            /** @default null */
            error: components["schemas"]["PublicError"] | null;
            /** @default null */
            result_ref: components["schemas"]["SessionResultReference"] | null;
            /**
             * Session Id
             * @default null
             */
            session_id: string | null;
            /** @default not_started */
            status: components["schemas"]["RunSummaryStatus"];
        };
        /** RunSummaryResult */
        RunSummaryResult: {
            /**
             * Generated At
             * Format: date-time
             */
            generated_at: string;
            /**
             * Highlights
             * @default []
             */
            highlights: components["schemas"]["RunSummaryHighlight"][];
            /** Input Digest */
            input_digest: string;
            /** Language */
            language: string;
            model: components["schemas"]["ModelSelection"];
            /** Plain Language Summary */
            plain_language_summary: string;
            /**
             * Run Id
             * Format: uuid
             */
            run_id: string;
            /**
             * Schema Version
             * @default 1
             * @constant
             */
            schema_version: 1;
            /**
             * Session Id
             * Format: uuid
             */
            session_id: string;
            /** @default null */
            usage: components["schemas"]["ModelUsageSummary"] | null;
        };
        /** RunSummarySessionResultSummary */
        RunSummarySessionResultSummary: {
            /** Highlight Count */
            highlight_count: number;
            /** Language */
            language: string;
            /**
             * Result Kind
             * @default run_summary
             * @constant
             */
            result_kind: "run_summary";
        };
        /**
         * RunSummaryStatus
         * @enum {string}
         */
        RunSummaryStatus: "not_started" | "running" | "completed" | "failed" | "cancelled";
        /** RunWaterbodySummary */
        RunWaterbodySummary: {
            /** @default null */
            dependency_status: components["schemas"]["DependencyStatus"] | null;
            /** Participating */
            participating: boolean;
            /** @default null */
            result_ref: components["schemas"]["SessionResultReference"] | null;
            scheduling_status: components["schemas"]["WaterbodySchedulingStatus"];
            /**
             * Session Id
             * @default null
             */
            session_id: string | null;
            /** @default null */
            session_status: components["schemas"]["AgentSessionStatus"] | null;
            /** Target Comid */
            target_comid: string;
        };
        /** SaveGuidelineDraftRequest */
        SaveGuidelineDraftRequest: {
            draft: components["schemas"]["GuidelineDraftV1"];
            /** Expected Revision */
            expected_revision: number;
        };
        /** SessionDetailResponse */
        SessionDetailResponse: {
            /**
             * Issues
             * @default []
             */
            issues: components["schemas"]["ReadIssue"][];
            persistence?: components["schemas"]["PersistenceInfo"] | null;
            /**
             * Run Id
             * Format: uuid
             */
            run_id: string;
            /** Session */
            session: components["schemas"]["SessionSummary"] | components["schemas"]["ReadSessionSummary"];
            source: components["schemas"]["ReadSource"];
            /** Transcript Url */
            transcript_url: string;
        };
        /** SessionProcessEvent */
        SessionProcessEvent: {
            /**
             * Occurred At
             * Format: date-time
             */
            occurred_at: string;
            /** Payload */
            payload: components["schemas"]["TranscriptItemPayload"] | components["schemas"]["MessageStartedPayload"] | components["schemas"]["MessageDeltaPayload"] | components["schemas"]["MessageFinalizedPayload"];
            /**
             * Run Id
             * Format: uuid
             */
            run_id: string;
            /**
             * Schema Version
             * @default 1
             * @constant
             */
            schema_version: 1;
            /** Sequence */
            sequence: number;
            /**
             * Session Id
             * Format: uuid
             */
            session_id: string;
        };
        /** SessionResultReference */
        SessionResultReference: {
            /**
             * Item Id
             * Format: uuid
             */
            item_id: string;
            /**
             * Run Id
             * Format: uuid
             */
            run_id: string;
        };
        /** SessionSummary */
        SessionSummary: {
            agent_role: components["schemas"]["AgentRole"];
            /**
             * Created At
             * Format: date-time
             */
            created_at: string;
            /** @default null */
            result_ref: components["schemas"]["SessionResultReference"] | null;
            /**
             * Result Summary
             * @default null
             */
            result_summary: (components["schemas"]["WaterbodySessionResultSummary"] | components["schemas"]["RunSummarySessionResultSummary"]) | null;
            /**
             * Session Id
             * Format: uuid
             */
            session_id: string;
            status: components["schemas"]["AgentSessionStatus"];
            /**
             * Target Comid
             * @default null
             */
            target_comid: string | null;
            /**
             * Updated At
             * Format: date-time
             */
            updated_at: string;
        };
        /** SessionTranscriptResponse */
        SessionTranscriptResponse: {
            /**
             * Issues
             * @default []
             */
            issues: components["schemas"]["ReadIssue"][];
            /**
             * Items
             * @default []
             */
            items: components["schemas"]["TranscriptItemView"][];
            persistence?: components["schemas"]["PersistenceInfo"] | null;
            /**
             * Referenced Items
             * @default []
             */
            referenced_items: components["schemas"]["TranscriptItemView"][];
            /**
             * Run Id
             * Format: uuid
             */
            run_id: string;
            /**
             * Session Id
             * Format: uuid
             */
            session_id: string;
            source: components["schemas"]["ReadSource"];
        };
        /** SetGuidelineEnabledRequest */
        SetGuidelineEnabledRequest: {
            /** Enabled */
            enabled: boolean;
            /** Expected Revision */
            expected_revision: number;
        };
        /** SettingsCatalogProjection */
        SettingsCatalogProjection: {
            /** Embedding Profiles */
            embedding_profiles: components["schemas"]["EmbeddingProfileOption"][];
            /** Iana Timezones */
            iana_timezones: string[];
            /** Memory Modes */
            memory_modes: components["schemas"]["MemoryMode"][];
            /** Models */
            models: components["schemas"]["ModelOption"][];
            /** Time Resolutions */
            time_resolutions: components["schemas"]["TimeResolutionPreference"][];
            /**
             * Unit Options
             * @default []
             */
            unit_options: components["schemas"]["UnitParameterOption"][];
        };
        /** SettingsConflictResponse */
        SettingsConflictResponse: {
            current_settings: components["schemas"]["SettingsResponse"];
            error: components["schemas"]["HttpPublicError"];
            /**
             * History Url
             * @default null
             */
            history_url: string | null;
            /** @default null */
            run: components["schemas"]["RunStatusResponse"] | null;
            /**
             * Validation
             * @default []
             */
            validation: components["schemas"]["ValidationViolation"][];
        };
        /** SettingsResponse */
        SettingsResponse: {
            catalog: components["schemas"]["SettingsCatalogProjection"];
            defaults: components["schemas"]["MutableDefaults"];
            /** Revision */
            revision: number;
            /**
             * Schema Version
             * @default 1
             * @constant
             */
            schema_version: 1;
            unit_policy: components["schemas"]["UnitPolicyProjection"];
            /** Updated At */
            updated_at: string | null;
        };
        /** SourceRecordReference */
        SourceRecordReference: {
            /** Item Id */
            item_id: string;
            /** Record Id */
            record_id: string;
        };
        /** StableSourceReference */
        StableSourceReference: {
            /**
             * Cell Id
             * @default null
             */
            cell_id: string | null;
            /** Content Schema Version */
            content_schema_version: number;
            /** Content Type */
            content_type: string;
            /**
             * Item Id
             * Format: uuid
             */
            item_id: string;
            /**
             * Result Id
             * @default null
             */
            result_id: string | null;
            /**
             * Run Id
             * Format: uuid
             */
            run_id: string;
            /**
             * Session Id
             * @default null
             */
            session_id: string | null;
            /**
             * Window Id
             * @default null
             */
            window_id: string | null;
        };
        /** StorageItemStatus */
        StorageItemStatus: {
            /**
             * State
             * @enum {string}
             */
            state: "available" | "unavailable" | "unknown" | "not_present";
        };
        /** StreamEndEvent */
        StreamEndEvent: {
            /**
             * Run Id
             * Format: uuid
             */
            run_id: string;
            /**
             * Schema Version
             * @default 1
             * @constant
             */
            schema_version: 1;
            /**
             * Session Id
             * @default null
             */
            session_id: string | null;
            /**
             * Stream
             * @enum {string}
             */
            stream: "run" | "session";
        };
        /**
         * SupportingIntentReason
         * @enum {string}
         */
        SupportingIntentReason: "system_required" | "capability_required";
        /** SupportingParameterIntent */
        SupportingParameterIntent: {
            /** Canonical Name */
            canonical_name: string;
            /** Canonical Unit */
            canonical_unit: string;
            key: components["schemas"]["ParameterKey"];
            reason: components["schemas"]["SupportingIntentReason"];
        };
        /**
         * TargetSource
         * @enum {string}
         */
        TargetSource: "frontend" | "query";
        /** TimeRange */
        TimeRange: {
            /**
             * End
             * Format: date-time
             */
            end: string;
            /**
             * Start
             * Format: date-time
             */
            start: string;
        };
        /**
         * TimeResolution
         * @enum {string}
         */
        TimeResolution: "6h" | "12h" | "24h";
        /**
         * TimeResolutionPreference
         * @enum {string}
         */
        TimeResolutionPreference: "auto" | "6h" | "12h" | "24h";
        /**
         * TimeResolutionPreferenceSource
         * @enum {string}
         */
        TimeResolutionPreferenceSource: "frontend" | "default";
        /**
         * TimeResolutionSource
         * @enum {string}
         */
        TimeResolutionSource: "frontend" | "default" | "auto";
        /** TimeSegment */
        TimeSegment: {
            /**
             * End
             * Format: date-time
             */
            end: string;
            mode: components["schemas"]["InferenceMode"];
            /**
             * Start
             * Format: date-time
             */
            start: string;
        };
        /** TimeWindowResult */
        TimeWindowResult: {
            aggregation_method: components["schemas"]["AggregationMethod"] | null;
            assessment: components["schemas"]["RiskOrTrendAssessment"];
            /** Cell Id */
            cell_id: string;
            /** Confidence */
            confidence: number;
            /** Confidence Rationale */
            confidence_rationale: string;
            /**
             * End Time
             * Format: date-time
             */
            end_time: string;
            /** Evidence Refs */
            evidence_refs: string[];
            /**
             * Inference Basis
             * @default null
             */
            inference_basis: ("same_parameter_measurement" | "related_evidence" | "general_knowledge") | null;
            /**
             * Inference Method
             * @default null
             */
            inference_method: ("explicit_calculation" | "model_estimate") | null;
            /**
             * Measured Anchors
             * @default []
             */
            measured_anchors: components["schemas"]["MeasuredAnchor"][];
            outcome_status: components["schemas"]["OutcomeStatus"];
            /**
             * Quality Flags
             * @default []
             */
            quality_flags: components["schemas"]["DataQualityFlag"][];
            /** Representative Value */
            representative_value: string | null;
            source: components["schemas"]["ResultSource"] | null;
            /**
             * Start Time
             * Format: date-time
             */
            start_time: string;
            /**
             * Temporal Estimate Method
             * @default null
             */
            temporal_estimate_method: ("linear_interpolation" | "nearest_hold") | null;
            /** Unit Id */
            unit_id: string | null;
            /**
             * Value Lower
             * @default null
             */
            value_lower: string | null;
            /**
             * Value Upper
             * @default null
             */
            value_upper: string | null;
        };
        /**
         * TopologyCoverage
         * @enum {string}
         */
        TopologyCoverage: "complete" | "partial" | "none";
        /** TopologyLimits */
        TopologyLimits: {
            /** Max Distance Km */
            max_distance_km: number;
            /** Max Hops */
            max_hops: number;
            /** Max Nodes */
            max_nodes: number;
        };
        /** TopologyRunResultSummary */
        TopologyRunResultSummary: {
            graph_coverage: components["schemas"]["GraphCoverage"];
            outcome_counts: components["schemas"]["OutcomeCounts"];
            summary_error?: components["schemas"]["PublicError"] | null;
            summary_result_ref?: components["schemas"]["SessionResultReference"] | null;
            /**
             * Summary Status
             * @enum {string}
             */
            summary_status: "completed" | "failed";
            /** Target Comid */
            target_comid: string;
            target_result_summary: components["schemas"]["WaterbodySessionResultSummary"];
        };
        /** TopologySearchStats */
        TopologySearchStats: {
            /** Discovered Nodes */
            discovered_nodes: number;
            /** Excluded Nodes */
            excluded_nodes: number;
            /** Max Depth */
            max_depth: number;
            /** Max Distance Km */
            max_distance_km: number;
            /** Qualified Inlets */
            qualified_inlets: number;
            /** Queried Nodes */
            queried_nodes: number;
        };
        /** TopologySeriesRef */
        TopologySeriesRef: {
            /** Channel */
            channel: string;
            /** Covered Segment Indexes */
            covered_segment_indexes: number[];
            /**
             * Covered Window Ids
             * @default null
             */
            covered_window_ids: string[] | null;
            parameter: components["schemas"]["ParameterKey"];
            /** Series Identity */
            series_identity: string;
        };
        /** TopologyStationRef */
        TopologyStationRef: {
            provider: components["schemas"]["ParameterProvider"];
            /** Series Refs */
            series_refs: components["schemas"]["TopologySeriesRef"][];
            /** Station Id */
            station_id: string;
        };
        /** TranscriptContentRef */
        TranscriptContentRef: {
            /**
             * Item Id
             * Format: uuid
             */
            item_id: string;
            /**
             * Run Id
             * Format: uuid
             */
            run_id: string;
        };
        /** TranscriptContentResponse */
        TranscriptContentResponse: {
            /** Content Bytes */
            content_bytes: number;
            /** Content Schema Version */
            content_schema_version: number;
            /** Content Type */
            content_type: string;
            /**
             * Issues
             * @default []
             */
            issues: components["schemas"]["ReadIssue"][];
            /**
             * Item Id
             * Format: uuid
             */
            item_id: string;
            persistence?: components["schemas"]["PersistenceInfo"] | null;
            /**
             * Run Id
             * Format: uuid
             */
            run_id: string;
            source: components["schemas"]["ReadSource"];
            /** Text */
            text: string;
        };
        /** TranscriptCorrelation */
        TranscriptCorrelation: {
            /**
             * Error Id
             * @default null
             */
            error_id: string | null;
            /**
             * Operation Id
             * @default null
             */
            operation_id: string | null;
            /**
             * Request Id
             * @default null
             */
            request_id: string | null;
        };
        /** TranscriptItemHeader */
        TranscriptItemHeader: {
            /** Content Schema Version */
            content_schema_version: number;
            /** Content Type */
            content_type: string;
            correlation: components["schemas"]["TranscriptCorrelation"];
            /**
             * Item Id
             * Format: uuid
             */
            item_id: string;
            /**
             * Occurred At
             * Format: date-time
             */
            occurred_at: string;
            /**
             * Rag Eligible
             * @default false
             */
            rag_eligible: boolean;
            /**
             * Run Id
             * Format: uuid
             */
            run_id: string;
            scope: components["schemas"]["TranscriptScope"];
            /**
             * Tags
             * @default []
             */
            tags: string[];
            /** Transcript Sequence */
            transcript_sequence: number;
        };
        /** TranscriptItemPayload */
        TranscriptItemPayload: {
            item: components["schemas"]["TranscriptItemView"];
        };
        /** TranscriptItemView */
        TranscriptItemView: {
            /**
             * Confidence
             * @default null
             */
            confidence: number | null;
            /** Content */
            content: components["schemas"]["InlineTranscriptContent"] | components["schemas"]["ReferencedTranscriptContent"] | components["schemas"]["UnparsedTranscriptContent"];
            /** Content Bytes */
            content_bytes: number;
            content_ref: components["schemas"]["TranscriptContentRef"];
            /** Content Schema Version */
            content_schema_version: number;
            /** Content Type */
            content_type: string;
            correlation: components["schemas"]["TranscriptCorrelation"];
            /**
             * Item Id
             * Format: uuid
             */
            item_id: string;
            /**
             * Occurred At
             * Format: date-time
             */
            occurred_at: string;
            /** @default null */
            parse_error: components["schemas"]["PublicError"] | null;
            parse_status: components["schemas"]["ParseStatus"];
            /**
             * Provenance
             * @default null
             */
            provenance: string[] | null;
            /**
             * Rag Eligible
             * @default false
             */
            rag_eligible: boolean;
            /**
             * Run Id
             * Format: uuid
             */
            run_id: string;
            scope: components["schemas"]["TranscriptScope"];
            /**
             * Tags
             * @default []
             */
            tags: string[];
            /** Transcript Sequence */
            transcript_sequence: number;
        };
        /** TranscriptScope */
        TranscriptScope: {
            /** @default null */
            agent_role: components["schemas"]["AgentRole"] | null;
            /**
             * Session Id
             * @default null
             */
            session_id: string | null;
            /**
             * Target Comid
             * @default null
             */
            target_comid: string | null;
        };
        /**
         * TrendDirection
         * @enum {string}
         */
        TrendDirection: "rising" | "falling" | "stable" | "uncertain";
        /** UnitCatalogChoice */
        UnitCatalogChoice: {
            /** Dimension Id */
            dimension_id: string;
            /** Symbol */
            symbol: string;
            /** Unit Id */
            unit_id: string;
        };
        /** UnitParameterOption */
        UnitParameterOption: {
            /** Name */
            name: string;
            parameter: components["schemas"]["ParameterKey"];
            /** Source Unit Id */
            source_unit_id: string;
            /** Target Units */
            target_units: components["schemas"]["UnitCatalogChoice"][];
        };
        /** UnitPolicyChange */
        UnitPolicyChange: {
            /** Dimension Id */
            dimension_id: string;
            parameter: components["schemas"]["ParameterKey"];
            /** Previous Unit Id */
            previous_unit_id: string | null;
            /** Target Unit Id */
            target_unit_id: string;
        };
        /** UnitPolicyEntry */
        UnitPolicyEntry: {
            parameter: components["schemas"]["ParameterKey"];
            /** Target Unit Id */
            target_unit_id: string;
        };
        /** UnitPolicyPreviewResponse */
        UnitPolicyPreviewResponse: {
            /** Affected Dimensions */
            affected_dimensions: string[];
            /** Base Version */
            base_version: number;
            /** Changes */
            changes: components["schemas"]["UnitPolicyChange"][];
            confirmation: components["schemas"]["AdminConfirmation"] | null;
            proposed: components["schemas"]["UnitPolicyProjection"];
            /** Registry Digest */
            registry_digest: string;
        };
        /** UnitPolicyProjection */
        UnitPolicyProjection: {
            /** Entries */
            entries: components["schemas"]["UnitPolicyEntry"][];
            /** Registry Digest */
            registry_digest: string;
            /** Version */
            version: number;
        };
        /** UnitPolicyResponse */
        UnitPolicyResponse: {
            /**
             * Created At
             * Format: date-time
             */
            created_at: string;
            /** Entries */
            entries: components["schemas"]["UnitPolicyEntry"][];
            /** Registry Digest */
            registry_digest: string;
            /** Version */
            version: number;
        };
        /** UnitPreference */
        UnitPreference: {
            parameter: components["schemas"]["ParameterKey"];
            /** Registry Digest */
            registry_digest: string;
            /**
             * Source
             * @enum {string}
             */
            source: "session_override" | "global_policy";
            /** Target Unit Id */
            target_unit_id: string;
        };
        /** UnparsedTranscriptContent */
        UnparsedTranscriptContent: {
            /**
             * @description discriminator enum property added by openapi-typescript
             * @enum {string}
             */
            kind: "unparsed";
        };
        /** ValidationViolation */
        ValidationViolation: {
            /** Field */
            field: (string | number)[];
            /** Message */
            message: string;
            /** Type */
            type: string;
        };
        /** WaterQualityParameter */
        WaterQualityParameter: {
            /** Canonical Unit */
            canonical_unit: string | null;
            /** Category */
            category: string | null;
            /**
             * Is Dimensionless
             * @default false
             */
            is_dimensionless: boolean;
            key: components["schemas"]["ParameterKey"];
            /** Name */
            name: string;
        };
        /** WaterbodyGeometry */
        WaterbodyGeometry: {
            /** Bbox */
            bbox: [
                number,
                number,
                number,
                number
            ];
            /** Full Coordinates */
            full_coordinates: [
                number,
                number
            ][];
            /** Geometry Ref */
            geometry_ref: string;
            /**
             * Geometry Type
             * @default linestring
             * @constant
             */
            geometry_type: "linestring";
            /** Length Km */
            length_km: string;
            representative_point: components["schemas"]["GeoPoint"];
            /** Simplification Tolerance M */
            simplification_tolerance_m: string;
            /** Simplified Coordinates */
            simplified_coordinates: [
                number,
                number
            ][];
            /**
             * Source
             * @default nldi
             * @constant
             */
            source: "nldi";
            /** Source Comid */
            source_comid: string;
        };
        /** WaterbodyGeometrySummary */
        WaterbodyGeometrySummary: {
            /** Bbox */
            bbox: [
                number,
                number,
                number,
                number
            ];
            /**
             * Geometry Type
             * @default linestring
             * @constant
             */
            geometry_type: "linestring";
            /** Length Km */
            length_km: string;
            /** Simplification Tolerance M */
            simplification_tolerance_m: string;
            /** Simplified Coordinates */
            simplified_coordinates: [
                number,
                number
            ][];
        };
        /**
         * WaterbodySchedulingStatus
         * @enum {string}
         */
        WaterbodySchedulingStatus: "excluded" | "waiting" | "ready" | "running" | "terminal";
        /** WaterbodySessionResultSummary */
        WaterbodySessionResultSummary: {
            availability: components["schemas"]["AssessmentAvailability"];
            /** Overall Confidence */
            overall_confidence: number;
            /** Result Id */
            result_id: string;
            /**
             * Result Kind
             * @default waterbody
             * @constant
             */
            result_kind: "waterbody";
            risk_level: components["schemas"]["RiskLevel"];
            /** Target Comid */
            target_comid: string;
        };
    };
    responses: never;
    parameters: never;
    requestBodies: never;
    headers: never;
    pathItems: never;
}
export type $defs = Record<string, never>;
export interface operations {
    execute_history_api_admin_history_cleanups_post: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": string;
            };
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["ConfirmActionRequest"];
            };
        };
        responses: {
            /** @description Successful Response */
            202: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AdminOperationSnapshot"];
                };
            };
            /** @description Forbidden */
            403: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Not Found */
            404: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Method Not Allowed */
            405: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Conflict */
            409: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Unprocessable Entity */
            422: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Internal Server Error */
            500: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
        };
    };
    preview_history_api_admin_history_cleanups_preview_post: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["HistoryCleanupPreviewRequest"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HistoryCleanupPreviewResponse"];
                };
            };
            /** @description Forbidden */
            403: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Method Not Allowed */
            405: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Conflict */
            409: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Unprocessable Entity */
            422: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Internal Server Error */
            500: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
        };
    };
    list_operations_api_admin_operations_get: {
        parameters: {
            query?: {
                limit?: number;
                cursor?: string | null;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AdminOperationListResponse"];
                };
            };
            /** @description Forbidden */
            403: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Method Not Allowed */
            405: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Unprocessable Entity */
            422: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Internal Server Error */
            500: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
        };
    };
    get_operation_api_admin_operations__operation_id__get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                operation_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AdminOperationSnapshot"];
                };
            };
            /** @description Forbidden */
            403: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Not Found */
            404: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Method Not Allowed */
            405: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Unprocessable Entity */
            422: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Internal Server Error */
            500: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
        };
    };
    get_rag_api_admin_rag_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AdminRAGStatusResponse"];
                };
            };
            /** @description Forbidden */
            403: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Method Not Allowed */
            405: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Unprocessable Entity */
            422: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Internal Server Error */
            500: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
        };
    };
    preview_deletion_api_admin_rag_generations__generation_id__deletion_preview_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                generation_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["GenerationPreviewResponse"];
                };
            };
            /** @description Forbidden */
            403: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Not Found */
            404: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Method Not Allowed */
            405: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Conflict */
            409: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Unprocessable Entity */
            422: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Internal Server Error */
            500: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
        };
    };
    execute_deletion_api_admin_rag_generations__generation_id__deletions_post: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": string;
            };
            path: {
                generation_id: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["ConfirmActionRequest"];
            };
        };
        responses: {
            /** @description Successful Response */
            202: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AdminOperationSnapshot"];
                };
            };
            /** @description Forbidden */
            403: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Not Found */
            404: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Method Not Allowed */
            405: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Conflict */
            409: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Unprocessable Entity */
            422: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Internal Server Error */
            500: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
        };
    };
    execute_retry_api_admin_rag_generations__generation_id__failed_jobs_retries_post: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": string;
            };
            path: {
                generation_id: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["ConfirmActionRequest"];
            };
        };
        responses: {
            /** @description Successful Response */
            202: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AdminOperationSnapshot"];
                };
            };
            /** @description Forbidden */
            403: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Not Found */
            404: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Method Not Allowed */
            405: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Conflict */
            409: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Unprocessable Entity */
            422: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Internal Server Error */
            500: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
        };
    };
    preview_retry_api_admin_rag_generations__generation_id__failed_jobs_retry_preview_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                generation_id: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["RetryFailedJobsPreviewRequest"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["GenerationPreviewResponse"];
                };
            };
            /** @description Forbidden */
            403: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Not Found */
            404: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Method Not Allowed */
            405: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Conflict */
            409: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Unprocessable Entity */
            422: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Internal Server Error */
            500: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
        };
    };
    execute_profile_api_admin_rag_profile_handoffs_post: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": string;
            };
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["ConfirmActionRequest"];
            };
        };
        responses: {
            /** @description Successful Response */
            202: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AdminOperationSnapshot"];
                };
            };
            /** @description Forbidden */
            403: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Not Found */
            404: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Method Not Allowed */
            405: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Conflict */
            409: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Unprocessable Entity */
            422: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Internal Server Error */
            500: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
        };
    };
    preview_profile_api_admin_rag_profile_handoffs_preview_post: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ProfileHandoffPreviewResponse"];
                };
            };
            /** @description Forbidden */
            403: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Method Not Allowed */
            405: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Conflict */
            409: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Unprocessable Entity */
            422: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Internal Server Error */
            500: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
        };
    };
    get_storage_api_admin_storage_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AdminStorageStatusResponse"];
                };
            };
            /** @description Forbidden */
            403: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Method Not Allowed */
            405: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Unprocessable Entity */
            422: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Internal Server Error */
            500: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
        };
    };
    list_runs_api_analysis_runs_get: {
        parameters: {
            query?: {
                limit?: number;
                cursor?: string | null;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RunHistoryResponse"];
                };
            };
            /** @description Method Not Allowed */
            405: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Unprocessable Entity */
            422: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Internal Server Error */
            500: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
        };
    };
    create_run_api_analysis_runs_post: {
        parameters: {
            query?: never;
            header?: {
                /** @description Optional client-generated UUID v4, reused with identical bytes. */
                "Idempotency-Key"?: string | null;
            };
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["CreateAnalysisRunRequest"];
            };
        };
        responses: {
            /** @description Successful Response */
            201: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunResponse"];
                };
            };
            /** @description Bad Request */
            400: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Method Not Allowed */
            405: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Conflict */
            409: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Gone */
            410: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Unprocessable Entity */
            422: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Internal Server Error */
            500: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
        };
    };
    get_run_api_analysis_runs__run_id__get: {
        parameters: {
            query?: {
                view?: components["schemas"]["ReadView"];
            };
            header?: never;
            path: {
                run_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RunDetailResponse"];
                };
            };
            /** @description Not Found */
            404: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Method Not Allowed */
            405: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Unprocessable Entity */
            422: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Internal Server Error */
            500: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
        };
    };
    cancel_run_api_analysis_runs__run_id__cancel_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                run_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RunStatusResponse"];
                };
            };
            /** @description Not Found */
            404: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Method Not Allowed */
            405: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Conflict */
            409: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Unprocessable Entity */
            422: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Internal Server Error */
            500: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
        };
    };
    subscribe_events_api_analysis_runs__run_id__events_get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                run_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Run snapshot SSE stream with comment keepalive. */
            200: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "text/event-stream": string;
                };
            };
            /** @description Not Found */
            404: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Method Not Allowed */
            405: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Gone */
            410: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Unprocessable Entity */
            422: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Internal Server Error */
            500: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
        };
    };
    retry_persistence_api_analysis_runs__run_id__persistence_retry_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                run_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description OK */
            200: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["PersistenceRetryResponse"];
                };
            };
            /** @description Successful Response */
            202: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["PersistenceRetryResponse"];
                };
            };
            /** @description Not Found */
            404: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Method Not Allowed */
            405: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Conflict */
            409: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Unprocessable Entity */
            422: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Internal Server Error */
            500: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
        };
    };
    get_session_api_analysis_runs__run_id__sessions__session_id__get: {
        parameters: {
            query?: {
                view?: components["schemas"]["ReadView"];
            };
            header?: never;
            path: {
                run_id: string;
                session_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["SessionDetailResponse"];
                };
            };
            /** @description Not Found */
            404: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Method Not Allowed */
            405: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Unprocessable Entity */
            422: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Internal Server Error */
            500: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
        };
    };
    subscribe_session_events_api_analysis_runs__run_id__sessions__session_id__events_get: {
        parameters: {
            query?: never;
            header?: {
                /** @description Non-negative ASCII decimal session event cursor. */
                "Last-Event-ID"?: string | null;
            };
            path: {
                run_id: string;
                session_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Session process SSE stream with comment keepalive. */
            200: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "text/event-stream": string;
                };
            };
            /** @description Bad Request */
            400: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Not Found */
            404: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Method Not Allowed */
            405: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Conflict */
            409: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Gone */
            410: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Unprocessable Entity */
            422: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Internal Server Error */
            500: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
        };
    };
    get_session_transcript_api_analysis_runs__run_id__sessions__session_id__transcript_get: {
        parameters: {
            query?: {
                view?: components["schemas"]["ReadView"];
            };
            header?: never;
            path: {
                run_id: string;
                session_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["SessionTranscriptResponse"];
                };
            };
            /** @description Not Found */
            404: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Method Not Allowed */
            405: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Unprocessable Entity */
            422: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Internal Server Error */
            500: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
        };
    };
    get_anchor_distance_estimates_api_analysis_runs__run_id__transcript__item_id__anchor_distance_estimates_get: {
        parameters: {
            query?: {
                view?: "history";
            };
            header?: never;
            path: {
                run_id: string;
                item_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnchorDistanceEstimatesResponse"];
                };
            };
            /** @description Not Found */
            404: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Method Not Allowed */
            405: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Unprocessable Entity */
            422: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Internal Server Error */
            500: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
        };
    };
    get_transcript_content_api_analysis_runs__run_id__transcript__item_id__content_get: {
        parameters: {
            query?: {
                view?: components["schemas"]["ReadView"];
            };
            header?: never;
            path: {
                run_id: string;
                item_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["TranscriptContentResponse"];
                };
            };
            /** @description Not Found */
            404: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Method Not Allowed */
            405: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Unprocessable Entity */
            422: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Internal Server Error */
            500: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
        };
    };
    list_guidelines_api_guidelines_get: {
        parameters: {
            query?: {
                source?: "all" | "bundled" | "mine";
                state?: ("draft" | "preparing" | "available" | "preparation_failed" | "disabled" | "unavailable") | null;
                q?: string;
                limit?: number;
                cursor?: string | null;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["GuidelineListResponse"];
                };
            };
            /** @description Bad Request */
            400: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Forbidden */
            403: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Not Found */
            404: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Method Not Allowed */
            405: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Conflict */
            409: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["GuidelineConflictResponse"];
                };
            };
            /** @description Unprocessable Entity */
            422: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Internal Server Error */
            500: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
        };
    };
    create_guideline_api_guidelines_post: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": string;
            };
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["GuidelineDraftV1"];
            };
        };
        responses: {
            /** @description Successful Response */
            201: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["GuidelineMutationReceipt"];
                };
            };
            /** @description Bad Request */
            400: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Forbidden */
            403: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Not Found */
            404: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Method Not Allowed */
            405: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Conflict */
            409: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["GuidelineConflictResponse"];
                };
            };
            /** @description Unprocessable Entity */
            422: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Internal Server Error */
            500: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
        };
    };
    get_guideline_operation_api_guidelines_operations__operation_id__get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                operation_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["GuidelineOperation"];
                };
            };
            /** @description Bad Request */
            400: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Forbidden */
            403: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Not Found */
            404: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Method Not Allowed */
            405: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Conflict */
            409: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["GuidelineConflictResponse"];
                };
            };
            /** @description Unprocessable Entity */
            422: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Internal Server Error */
            500: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
        };
    };
    retry_guideline_operation_api_guidelines_operations__operation_id__retry_post: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": string;
            };
            path: {
                operation_id: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["RetryGuidelineOperationRequest"];
            };
        };
        responses: {
            /** @description Successful Response */
            202: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["GuidelineMutationReceipt"];
                };
            };
            /** @description Bad Request */
            400: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Forbidden */
            403: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Not Found */
            404: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Method Not Allowed */
            405: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Conflict */
            409: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["GuidelineConflictResponse"];
                };
            };
            /** @description Unprocessable Entity */
            422: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Internal Server Error */
            500: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
        };
    };
    get_guideline_api_guidelines__document_id__get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                document_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["GuidelineDocumentDetails"];
                };
            };
            /** @description Bad Request */
            400: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Forbidden */
            403: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Not Found */
            404: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Method Not Allowed */
            405: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Conflict */
            409: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["GuidelineConflictResponse"];
                };
            };
            /** @description Unprocessable Entity */
            422: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Internal Server Error */
            500: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
        };
    };
    save_guideline_draft_api_guidelines__document_id__draft_put: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": string;
            };
            path: {
                document_id: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["SaveGuidelineDraftRequest"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["GuidelineMutationReceipt"];
                };
            };
            /** @description Bad Request */
            400: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Forbidden */
            403: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Not Found */
            404: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Method Not Allowed */
            405: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Conflict */
            409: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["GuidelineConflictResponse"];
                };
            };
            /** @description Unprocessable Entity */
            422: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Internal Server Error */
            500: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
        };
    };
    local_edit_guideline_api_guidelines__document_id__local_edit_post: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": string;
            };
            path: {
                document_id: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["LocalEditGuidelineRequest"];
            };
        };
        responses: {
            /** @description OK */
            200: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["GuidelineMutationReceipt"];
                };
            };
            /** @description Successful Response */
            201: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["GuidelineMutationReceipt"];
                };
            };
            /** @description Bad Request */
            400: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Forbidden */
            403: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Not Found */
            404: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Method Not Allowed */
            405: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Conflict */
            409: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["GuidelineConflictResponse"];
                };
            };
            /** @description Unprocessable Entity */
            422: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Internal Server Error */
            500: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
        };
    };
    publish_guideline_api_guidelines__document_id__publish_post: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": string;
            };
            path: {
                document_id: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["PublishGuidelineRequest"];
            };
        };
        responses: {
            /** @description Successful Response */
            202: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["GuidelineMutationReceipt"];
                };
            };
            /** @description Bad Request */
            400: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Forbidden */
            403: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Not Found */
            404: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Method Not Allowed */
            405: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Conflict */
            409: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["GuidelineConflictResponse"];
                };
            };
            /** @description Unprocessable Entity */
            422: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Internal Server Error */
            500: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
        };
    };
    restore_bundled_guideline_api_guidelines__document_id__restore_bundled_post: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": string;
            };
            path: {
                document_id: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["RestoreBundledGuidelineRequest"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["GuidelineMutationReceipt"];
                };
            };
            /** @description Bad Request */
            400: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Forbidden */
            403: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Not Found */
            404: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Method Not Allowed */
            405: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Conflict */
            409: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["GuidelineConflictResponse"];
                };
            };
            /** @description Unprocessable Entity */
            422: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Internal Server Error */
            500: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
        };
    };
    set_guideline_enabled_api_guidelines__document_id__set_enabled_post: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": string;
            };
            path: {
                document_id: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["SetGuidelineEnabledRequest"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["GuidelineMutationReceipt"];
                };
            };
            /** @description Bad Request */
            400: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Forbidden */
            403: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Not Found */
            404: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Method Not Allowed */
            405: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Conflict */
            409: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["GuidelineConflictResponse"];
                };
            };
            /** @description Unprocessable Entity */
            422: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Internal Server Error */
            500: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
        };
    };
    get_guideline_version_api_guidelines__document_id__versions__version_id__get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                document_id: string;
                version_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["GuidelineVersionV1"];
                };
            };
            /** @description Bad Request */
            400: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Forbidden */
            403: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Not Found */
            404: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Method Not Allowed */
            405: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Conflict */
            409: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["GuidelineConflictResponse"];
                };
            };
            /** @description Unprocessable Entity */
            422: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Internal Server Error */
            500: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
        };
    };
    list_keys_api_keys_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["KeyStatusResponse"];
                };
            };
            /** @description Forbidden */
            403: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Method Not Allowed */
            405: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Unprocessable Entity */
            422: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Internal Server Error */
            500: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
        };
    };
    save_key_api_keys__key_name__put: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                key_name: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["PutCredentialRequest"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["KeyStatusItem"];
                };
            };
            /** @description Forbidden */
            403: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Not Found */
            404: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Method Not Allowed */
            405: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Unprocessable Entity */
            422: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Internal Server Error */
            500: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
        };
    };
    delete_key_api_keys__key_name__delete: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                key_name: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["KeyStatusItem"];
                };
            };
            /** @description Forbidden */
            403: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Not Found */
            404: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Method Not Allowed */
            405: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Unprocessable Entity */
            422: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Internal Server Error */
            500: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
        };
    };
    test_key_api_keys__key_name__test_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                key_name: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["CredentialTestResponse"];
                };
            };
            /** @description Forbidden */
            403: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Not Found */
            404: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Method Not Allowed */
            405: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Conflict */
            409: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Unprocessable Entity */
            422: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Internal Server Error */
            500: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
        };
    };
    get_settings_api_settings_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["SettingsResponse"];
                };
            };
            /** @description Method Not Allowed */
            405: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Unprocessable Entity */
            422: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Internal Server Error */
            500: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
        };
    };
    replace_settings_api_settings_put: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["ReplaceSettingsRequest"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["SettingsResponse"];
                };
            };
            /** @description Method Not Allowed */
            405: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Conflict */
            409: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["SettingsConflictResponse"];
                };
            };
            /** @description Unprocessable Entity */
            422: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Internal Server Error */
            500: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
        };
    };
    confirm_unit_policy_api_settings_unit_policies_post: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": string;
            };
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["ConfirmActionRequest"];
            };
        };
        responses: {
            /** @description Successful Response */
            201: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["UnitPolicyResponse"];
                };
            };
            /** @description Method Not Allowed */
            405: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Conflict */
            409: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Unprocessable Entity */
            422: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Internal Server Error */
            500: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
        };
    };
    preview_unit_policy_api_settings_unit_policies_preview_post: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["PreviewUnitPolicyRequest"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["UnitPolicyPreviewResponse"];
                };
            };
            /** @description Method Not Allowed */
            405: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Conflict */
            409: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Unprocessable Entity */
            422: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Internal Server Error */
            500: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Service Unavailable */
            503: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
        };
    };
    health_health_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        [key: string]: string;
                    };
                };
            };
            /** @description Method Not Allowed */
            405: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
            /** @description Internal Server Error */
            500: {
                headers: {
                    /** @description Server-generated request UUID. */
                    "X-Request-ID"?: string;
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AnalysisRunErrorResponse"];
                };
            };
        };
    };
}
