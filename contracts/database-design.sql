-- Design-only MySQL 8.0.16+ / InnoDB DDL. Run in a new empty schema.

-- Application transactions enforce documented cross-resource and JSON invariants.

SET time_zone = '+00:00';

CREATE TABLE `tenants` (
  `id` CHAR(36) NOT NULL,
  `created_at` DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `updated_at` DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `version` INT UNSIGNED NOT NULL DEFAULT 1,
  `code` VARCHAR(64) NOT NULL,
  `name` VARCHAR(200) NOT NULL,
  `timezone` VARCHAR(64) NOT NULL,
  `status` ENUM('active','suspended') NOT NULL DEFAULT 'active',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_tenants_0` (`code`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE `roles` (
  `id` CHAR(36) NOT NULL,
  `created_at` DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `updated_at` DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `version` INT UNSIGNED NOT NULL DEFAULT 1,
  `code` VARCHAR(40) NOT NULL,
  `name` VARCHAR(100) NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_roles_0` (`code`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE `colleges` (
  `id` CHAR(36) NOT NULL,
  `created_at` DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `updated_at` DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `version` INT UNSIGNED NOT NULL DEFAULT 1,
  `tenant_id` CHAR(36) NOT NULL,
  `code` VARCHAR(64) NOT NULL,
  `name` VARCHAR(200) NOT NULL,
  `status` ENUM('active','archived') NOT NULL DEFAULT 'active',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_colleges_0` (`tenant_id`, `id`),
  UNIQUE KEY `uq_colleges_1` (`tenant_id`, `code`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE `laboratories` (
  `id` CHAR(36) NOT NULL,
  `created_at` DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `updated_at` DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `version` INT UNSIGNED NOT NULL DEFAULT 1,
  `tenant_id` CHAR(36) NOT NULL,
  `college_id` CHAR(36) NOT NULL,
  `code` VARCHAR(64) NOT NULL,
  `name` VARCHAR(200) NOT NULL,
  `status` ENUM('active','archived') NOT NULL DEFAULT 'active',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_laboratories_0` (`tenant_id`, `id`),
  UNIQUE KEY `uq_laboratories_1` (`tenant_id`, `code`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE `users` (
  `id` CHAR(36) NOT NULL,
  `created_at` DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `updated_at` DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `version` INT UNSIGNED NOT NULL DEFAULT 1,
  `tenant_id` CHAR(36) NOT NULL,
  `username` VARCHAR(128) NOT NULL,
  `display_name` VARCHAR(100) NOT NULL,
  `password_hash` VARCHAR(255) NOT NULL,
  `status` ENUM('active','disabled') NOT NULL DEFAULT 'active',
  `session_epoch` INT UNSIGNED NOT NULL DEFAULT 0,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_users_0` (`tenant_id`, `id`),
  UNIQUE KEY `uq_users_1` (`tenant_id`, `username`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE `user_roles` (
  `id` CHAR(36) NOT NULL,
  `created_at` DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `updated_at` DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `version` INT UNSIGNED NOT NULL DEFAULT 1,
  `tenant_id` CHAR(36) NOT NULL,
  `user_id` CHAR(36) NOT NULL,
  `role_id` CHAR(36) NOT NULL,
  `laboratory_id` CHAR(36) NULL,
  `scope_kind` ENUM('tenant','laboratory') NOT NULL DEFAULT 'tenant',
  `scope_key` CHAR(36) GENERATED ALWAYS AS (COALESCE(laboratory_id,'00000000-0000-0000-0000-000000000000')) STORED,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_user_roles_0` (`tenant_id`, `id`),
  UNIQUE KEY `uq_user_roles_1` (`tenant_id`, `user_id`, `role_id`, `scope_key`),
  CONSTRAINT `ck_user_roles_0` CHECK ((scope_kind='tenant' AND laboratory_id IS NULL) OR (scope_kind='laboratory' AND laboratory_id IS NOT NULL))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE `sessions` (
  `id` CHAR(36) NOT NULL,
  `created_at` DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `updated_at` DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `version` INT UNSIGNED NOT NULL DEFAULT 1,
  `tenant_id` CHAR(36) NOT NULL,
  `user_id` CHAR(36) NOT NULL,
  `token_hash` CHAR(64) NOT NULL,
  `csrf_hash` CHAR(64) NOT NULL,
  `session_epoch` INT UNSIGNED NOT NULL DEFAULT 0,
  `expires_at` DATETIME(3) NOT NULL,
  `idle_expires_at` DATETIME(3) NOT NULL,
  `revoked_at` DATETIME(3) NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_sessions_0` (`tenant_id`, `id`),
  UNIQUE KEY `uq_sessions_1` (`token_hash`),
  KEY `ix_sessions_0` (`expires_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE `locations` (
  `id` CHAR(36) NOT NULL,
  `created_at` DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `updated_at` DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `version` INT UNSIGNED NOT NULL DEFAULT 1,
  `tenant_id` CHAR(36) NOT NULL,
  `laboratory_id` CHAR(36) NOT NULL,
  `parent_id` CHAR(36) NULL,
  `type` ENUM('room','area','shelf','cabinet') NOT NULL DEFAULT 'room',
  `label` VARCHAR(200) NOT NULL,
  `status` ENUM('active','archived') NOT NULL DEFAULT 'active',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_locations_0` (`tenant_id`, `id`),
  UNIQUE KEY `uq_locations_1` (`tenant_id`, `laboratory_id`, `id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE `inspection_templates` (
  `id` CHAR(36) NOT NULL,
  `created_at` DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `updated_at` DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `version` INT UNSIGNED NOT NULL DEFAULT 1,
  `tenant_id` CHAR(36) NOT NULL,
  `family_id` CHAR(36) NOT NULL,
  `revision` INT UNSIGNED NOT NULL DEFAULT 1,
  `name` VARCHAR(200) NOT NULL,
  `status` ENUM('draft','published','retired') NOT NULL DEFAULT 'draft',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_inspection_templates_0` (`tenant_id`, `id`),
  UNIQUE KEY `uq_inspection_templates_1` (`tenant_id`, `family_id`, `revision`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE `template_items` (
  `id` CHAR(36) NOT NULL,
  `created_at` DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `updated_at` DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `version` INT UNSIGNED NOT NULL DEFAULT 1,
  `tenant_id` CHAR(36) NOT NULL,
  `template_id` CHAR(36) NOT NULL,
  `code` VARCHAR(64) NOT NULL,
  `title` VARCHAR(200) NOT NULL,
  `capture_hint` VARCHAR(1000) NOT NULL,
  `sort_order` INT UNSIGNED NOT NULL DEFAULT 0,
  `required` BOOLEAN NOT NULL DEFAULT TRUE,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_template_items_0` (`tenant_id`, `id`),
  UNIQUE KEY `uq_template_items_1` (`tenant_id`, `template_id`, `code`),
  UNIQUE KEY `uq_template_items_2` (`tenant_id`, `template_id`, `id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE `inspections` (
  `id` CHAR(36) NOT NULL,
  `created_at` DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `updated_at` DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `version` INT UNSIGNED NOT NULL DEFAULT 1,
  `tenant_id` CHAR(36) NOT NULL,
  `laboratory_id` CHAR(36) NOT NULL,
  `template_id` CHAR(36) NOT NULL,
  `inspector_id` CHAR(36) NOT NULL,
  `status` ENUM('draft','in_progress','completed','cancelled') NOT NULL DEFAULT 'draft',
  `completed_at` DATETIME(3) NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_inspections_0` (`tenant_id`, `id`),
  UNIQUE KEY `uq_inspections_1` (`tenant_id`, `laboratory_id`, `id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE `inspection_items` (
  `id` CHAR(36) NOT NULL,
  `created_at` DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `updated_at` DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `version` INT UNSIGNED NOT NULL DEFAULT 1,
  `tenant_id` CHAR(36) NOT NULL,
  `inspection_id` CHAR(36) NOT NULL,
  `laboratory_id` CHAR(36) NOT NULL,
  `template_item_id` CHAR(36) NOT NULL,
  `location_id` CHAR(36) NOT NULL,
  `status` ENUM('draft','uploaded','queued','quality_checking','needs_retake','processing','needs_review','completed','failed') NOT NULL DEFAULT 'draft',
  `submission_revision` INT UNSIGNED NOT NULL DEFAULT 0,
  `current_run_id` CHAR(36) NULL,
  `current_fact_revision_id` CHAR(36) NULL,
  `current_evaluation_id` CHAR(36) NULL,
  `review_outcome` ENUM('no_issue','issues_confirmed','cannot_determine') NULL,
  `reviewed_by` CHAR(36) NULL,
  `reviewed_at` DATETIME(3) NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_inspection_items_0` (`tenant_id`, `id`),
  UNIQUE KEY `uq_inspection_items_1` (`tenant_id`, `laboratory_id`, `id`),
  UNIQUE KEY `uq_inspection_items_2` (`tenant_id`, `inspection_id`, `template_item_id`, `location_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE `dictionary_versions` (
  `id` CHAR(36) NOT NULL,
  `created_at` DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `updated_at` DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `version` INT UNSIGNED NOT NULL DEFAULT 1,
  `tenant_id` CHAR(36) NOT NULL,
  `version_label` VARCHAR(64) NOT NULL,
  `checksum` CHAR(64) NOT NULL,
  `object_key` VARCHAR(1024) NOT NULL,
  `source` VARCHAR(2000) NOT NULL,
  `status` ENUM('draft','published','retired') NOT NULL DEFAULT 'draft',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_dictionary_versions_0` (`tenant_id`, `id`),
  UNIQUE KEY `uq_dictionary_versions_1` (`tenant_id`, `version_label`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE `chemical_entities` (
  `id` CHAR(36) NOT NULL,
  `created_at` DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `updated_at` DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `version` INT UNSIGNED NOT NULL DEFAULT 1,
  `tenant_id` CHAR(36) NOT NULL,
  `dictionary_version_id` CHAR(36) NOT NULL,
  `canonical_name` VARCHAR(200) NOT NULL,
  `cas_number` VARCHAR(32) NULL,
  `hazard_class` VARCHAR(80) NOT NULL,
  `storage_class` VARCHAR(80) NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_chemical_entities_0` (`tenant_id`, `id`),
  UNIQUE KEY `uq_chemical_entities_1` (`tenant_id`, `dictionary_version_id`, `id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE `chemical_aliases` (
  `id` CHAR(36) NOT NULL,
  `created_at` DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `updated_at` DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `version` INT UNSIGNED NOT NULL DEFAULT 1,
  `tenant_id` CHAR(36) NOT NULL,
  `dictionary_version_id` CHAR(36) NOT NULL,
  `entity_id` CHAR(36) NOT NULL,
  `normalized_alias` VARCHAR(200) NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_chemical_aliases_0` (`tenant_id`, `id`),
  UNIQUE KEY `uq_chemical_aliases_1` (`tenant_id`, `dictionary_version_id`, `entity_id`, `normalized_alias`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE `model_versions` (
  `id` CHAR(36) NOT NULL,
  `created_at` DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `updated_at` DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `version` INT UNSIGNED NOT NULL DEFAULT 1,
  `tenant_id` CHAR(36) NOT NULL,
  `name` VARCHAR(100) NOT NULL,
  `bundle_version` VARCHAR(64) NOT NULL,
  `dictionary_version_id` CHAR(36) NOT NULL,
  `checksum` CHAR(64) NOT NULL,
  `manifest` JSON NOT NULL,
  `status` ENUM('registered','validated','published','retired') NOT NULL DEFAULT 'registered',
  `evaluation_report_key` VARCHAR(1024) NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_model_versions_0` (`tenant_id`, `id`),
  UNIQUE KEY `uq_model_versions_1` (`tenant_id`, `bundle_version`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE `rule_sets` (
  `id` CHAR(36) NOT NULL,
  `created_at` DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `updated_at` DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `version` INT UNSIGNED NOT NULL DEFAULT 1,
  `tenant_id` CHAR(36) NOT NULL,
  `name` VARCHAR(200) NOT NULL,
  `description` VARCHAR(2000) NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_rule_sets_0` (`tenant_id`, `id`),
  UNIQUE KEY `uq_rule_sets_1` (`tenant_id`, `name`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE `rule_versions` (
  `id` CHAR(36) NOT NULL,
  `created_at` DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `updated_at` DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `version` INT UNSIGNED NOT NULL DEFAULT 1,
  `tenant_id` CHAR(36) NOT NULL,
  `rule_set_id` CHAR(36) NOT NULL,
  `revision` INT UNSIGNED NOT NULL DEFAULT 1,
  `checksum` CHAR(64) NOT NULL,
  `definition` JSON NOT NULL,
  `status` ENUM('draft','submitted','approved','published','retired') NOT NULL DEFAULT 'draft',
  `submitted_by` CHAR(36) NULL,
  `approved_by` CHAR(36) NULL,
  `approved_at` DATETIME(3) NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_rule_versions_0` (`tenant_id`, `id`),
  UNIQUE KEY `uq_rule_versions_1` (`tenant_id`, `rule_set_id`, `revision`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE `rule_bundles` (
  `id` CHAR(36) NOT NULL,
  `created_at` DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `updated_at` DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `version` INT UNSIGNED NOT NULL DEFAULT 1,
  `tenant_id` CHAR(36) NOT NULL,
  `checksum` CHAR(64) NOT NULL,
  `evaluator_version` VARCHAR(64) NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_rule_bundles_0` (`tenant_id`, `id`),
  UNIQUE KEY `uq_rule_bundles_1` (`tenant_id`, `checksum`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE `rule_bundle_members` (
  `id` CHAR(36) NOT NULL,
  `created_at` DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `updated_at` DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `version` INT UNSIGNED NOT NULL DEFAULT 1,
  `tenant_id` CHAR(36) NOT NULL,
  `bundle_id` CHAR(36) NOT NULL,
  `rule_version_id` CHAR(36) NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_rule_bundle_members_0` (`tenant_id`, `id`),
  UNIQUE KEY `uq_rule_bundle_members_1` (`tenant_id`, `bundle_id`, `rule_version_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE `activations` (
  `id` CHAR(36) NOT NULL,
  `created_at` DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `updated_at` DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `version` INT UNSIGNED NOT NULL DEFAULT 1,
  `tenant_id` CHAR(36) NOT NULL,
  `laboratory_id` CHAR(36) NOT NULL,
  `model_bundle_id` CHAR(36) NOT NULL,
  `dictionary_version_id` CHAR(36) NOT NULL,
  `rule_bundle_id` CHAR(36) NOT NULL,
  `pipeline_version` VARCHAR(64) NOT NULL,
  `device_profile` ENUM('cpu','cuda') NOT NULL DEFAULT 'cpu',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_activations_0` (`tenant_id`, `id`),
  UNIQUE KEY `uq_activations_1` (`tenant_id`, `laboratory_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE `inference_runs` (
  `id` CHAR(36) NOT NULL,
  `created_at` DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `updated_at` DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `version` INT UNSIGNED NOT NULL DEFAULT 1,
  `tenant_id` CHAR(36) NOT NULL,
  `item_id` CHAR(36) NOT NULL,
  `laboratory_id` CHAR(36) NOT NULL,
  `submission_revision` INT UNSIGNED NOT NULL DEFAULT 1,
  `replay_of` CHAR(36) NULL,
  `input_hash` CHAR(64) NOT NULL,
  `model_bundle_id` CHAR(36) NOT NULL,
  `dictionary_version_id` CHAR(36) NOT NULL,
  `rule_bundle_id` CHAR(36) NOT NULL,
  `pipeline_version` VARCHAR(64) NOT NULL,
  `device_profile` ENUM('cpu','cuda') NOT NULL DEFAULT 'cpu',
  `reference_date` DATE NOT NULL,
  `status` ENUM('queued','processing','retrying','completed','needs_retake','needs_review','failed','superseded') NOT NULL DEFAULT 'queued',
  `stage` ENUM('queued','quality','facts','rules','done') NOT NULL DEFAULT 'queued',
  `result` JSON NULL,
  `result_hash` CHAR(64) NULL,
  `error_code` VARCHAR(80) NULL,
  `started_at` DATETIME(3) NULL,
  `finished_at` DATETIME(3) NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_inference_runs_0` (`tenant_id`, `id`),
  UNIQUE KEY `uq_inference_runs_1` (`tenant_id`, `item_id`, `id`),
  UNIQUE KEY `uq_inference_runs_2` (`tenant_id`, `item_id`, `submission_revision`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE `fact_revisions` (
  `id` CHAR(36) NOT NULL,
  `created_at` DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `updated_at` DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `version` INT UNSIGNED NOT NULL DEFAULT 1,
  `tenant_id` CHAR(36) NOT NULL,
  `item_id` CHAR(36) NOT NULL,
  `run_id` CHAR(36) NOT NULL,
  `revision` INT UNSIGNED NOT NULL DEFAULT 1,
  `entities` JSON NOT NULL,
  `relations` JSON NOT NULL,
  `dates` JSON NOT NULL,
  `reason` VARCHAR(2000) NOT NULL,
  `created_by` CHAR(36) NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_fact_revisions_0` (`tenant_id`, `id`),
  UNIQUE KEY `uq_fact_revisions_1` (`tenant_id`, `item_id`, `id`),
  UNIQUE KEY `uq_fact_revisions_2` (`tenant_id`, `run_id`, `revision`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE `rule_evaluations` (
  `id` CHAR(36) NOT NULL,
  `created_at` DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `updated_at` DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `version` INT UNSIGNED NOT NULL DEFAULT 1,
  `tenant_id` CHAR(36) NOT NULL,
  `item_id` CHAR(36) NOT NULL,
  `run_id` CHAR(36) NOT NULL,
  `fact_revision_id` CHAR(36) NOT NULL,
  `rule_bundle_id` CHAR(36) NOT NULL,
  `reference_date` DATE NOT NULL,
  `status` ENUM('queued','running','completed','failed','superseded') NOT NULL DEFAULT 'queued',
  `result` JSON NULL,
  `result_hash` CHAR(64) NULL,
  `finished_at` DATETIME(3) NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_rule_evaluations_0` (`tenant_id`, `id`),
  UNIQUE KEY `uq_rule_evaluations_1` (`tenant_id`, `item_id`, `id`),
  UNIQUE KEY `uq_rule_evaluations_2` (`tenant_id`, `fact_revision_id`, `rule_bundle_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE `findings` (
  `id` CHAR(36) NOT NULL,
  `created_at` DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `updated_at` DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `version` INT UNSIGNED NOT NULL DEFAULT 1,
  `tenant_id` CHAR(36) NOT NULL,
  `item_id` CHAR(36) NOT NULL,
  `run_id` CHAR(36) NOT NULL,
  `fact_revision_id` CHAR(36) NOT NULL,
  `rule_evaluation_id` CHAR(36) NOT NULL,
  `rule_id` VARCHAR(80) NOT NULL,
  `fingerprint` CHAR(64) NOT NULL,
  `type` VARCHAR(80) NOT NULL,
  `severity` ENUM('low','medium','high','critical') NOT NULL DEFAULT 'low',
  `status` ENUM('needs_review','confirmed','rejected','cannot_determine','dispatched','closed') NOT NULL DEFAULT 'needs_review',
  `explanation` VARCHAR(4000) NOT NULL,
  `evidence` JSON NOT NULL,
  `superseded_at` DATETIME(3) NULL,
  `confirmed_by` CHAR(36) NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_findings_0` (`tenant_id`, `id`),
  UNIQUE KEY `uq_findings_1` (`tenant_id`, `rule_evaluation_id`, `fingerprint`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE `review_actions` (
  `id` CHAR(36) NOT NULL,
  `created_at` DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `updated_at` DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `version` INT UNSIGNED NOT NULL DEFAULT 1,
  `tenant_id` CHAR(36) NOT NULL,
  `actor_id` CHAR(36) NOT NULL,
  `resource_type` VARCHAR(80) NOT NULL,
  `resource_id` CHAR(36) NOT NULL,
  `action` VARCHAR(80) NOT NULL,
  `reason` VARCHAR(2000) NOT NULL,
  `before_version` INT UNSIGNED NOT NULL DEFAULT 1,
  `after_version` INT UNSIGNED NOT NULL DEFAULT 1,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_review_actions_0` (`tenant_id`, `id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE `remediation_tasks` (
  `id` CHAR(36) NOT NULL,
  `created_at` DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `updated_at` DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `version` INT UNSIGNED NOT NULL DEFAULT 1,
  `tenant_id` CHAR(36) NOT NULL,
  `finding_id` CHAR(36) NOT NULL,
  `laboratory_id` CHAR(36) NOT NULL,
  `assignee_id` CHAR(36) NOT NULL,
  `status` ENUM('pending_dispatch','in_progress','pending_recheck','rejected','cannot_remediate','closed') NOT NULL DEFAULT 'pending_dispatch',
  `priority` ENUM('low','medium','high','critical') NOT NULL DEFAULT 'low',
  `due_at` DATETIME(3) NOT NULL,
  `description` VARCHAR(4000) NOT NULL,
  `latest_evidence_id` CHAR(36) NULL,
  `closed_at` DATETIME(3) NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_remediation_tasks_0` (`tenant_id`, `id`),
  UNIQUE KEY `uq_remediation_tasks_1` (`tenant_id`, `finding_id`),
  UNIQUE KEY `uq_remediation_tasks_2` (`tenant_id`, `laboratory_id`, `id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE `remediation_evidence` (
  `id` CHAR(36) NOT NULL,
  `created_at` DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `updated_at` DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `version` INT UNSIGNED NOT NULL DEFAULT 1,
  `tenant_id` CHAR(36) NOT NULL,
  `task_id` CHAR(36) NOT NULL,
  `description` VARCHAR(4000) NOT NULL,
  `submitted_by` CHAR(36) NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_remediation_evidence_0` (`tenant_id`, `id`),
  UNIQUE KEY `uq_remediation_evidence_1` (`tenant_id`, `task_id`, `id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE `uploads` (
  `id` CHAR(36) NOT NULL,
  `created_at` DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `updated_at` DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `version` INT UNSIGNED NOT NULL DEFAULT 1,
  `tenant_id` CHAR(36) NOT NULL,
  `laboratory_id` CHAR(36) NOT NULL,
  `inspection_item_id` CHAR(36) NULL,
  `remediation_task_id` CHAR(36) NULL,
  `requested_by` CHAR(36) NOT NULL,
  `object_key` VARCHAR(1024) NOT NULL,
  `expected_sha256` CHAR(64) NOT NULL,
  `mime_type` VARCHAR(40) NOT NULL,
  `size_bytes` BIGINT UNSIGNED NOT NULL,
  `captured_at` DATETIME(3) NOT NULL,
  `expires_at` DATETIME(3) NOT NULL,
  `status` ENUM('granted','validating','ready','rejected','expired') NOT NULL DEFAULT 'granted',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_uploads_0` (`tenant_id`, `id`),
  CONSTRAINT `ck_uploads_0` CHECK ((inspection_item_id IS NULL) <> (remediation_task_id IS NULL))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE `asset_images` (
  `id` CHAR(36) NOT NULL,
  `created_at` DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `updated_at` DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `version` INT UNSIGNED NOT NULL DEFAULT 1,
  `tenant_id` CHAR(36) NOT NULL,
  `laboratory_id` CHAR(36) NOT NULL,
  `inspection_item_id` CHAR(36) NULL,
  `remediation_task_id` CHAR(36) NULL,
  `upload_id` CHAR(36) NOT NULL,
  `status` ENUM('validating','ready','rejected','deleted') NOT NULL DEFAULT 'validating',
  `original_key` VARCHAR(1024) NOT NULL,
  `original_object_version` VARCHAR(200) NOT NULL,
  `original_sha256` CHAR(64) NOT NULL,
  `analysis_key` VARCHAR(1024) NULL,
  `analysis_object_version` VARCHAR(200) NULL,
  `analysis_sha256` CHAR(64) NULL,
  `width` INT UNSIGNED NULL,
  `height` INT UNSIGNED NULL,
  `mime_type` VARCHAR(40) NOT NULL,
  `captured_at` DATETIME(3) NOT NULL,
  `legal_hold` BOOLEAN NOT NULL DEFAULT FALSE,
  `deleted_at` DATETIME(3) NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_asset_images_0` (`tenant_id`, `id`),
  UNIQUE KEY `uq_asset_images_1` (`tenant_id`, `upload_id`),
  UNIQUE KEY `uq_asset_images_2` (`tenant_id`, `inspection_item_id`, `id`),
  UNIQUE KEY `uq_asset_images_3` (`tenant_id`, `remediation_task_id`, `id`),
  CONSTRAINT `ck_asset_images_0` CHECK ((inspection_item_id IS NULL) <> (remediation_task_id IS NULL))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE `evidence_images` (
  `id` CHAR(36) NOT NULL,
  `created_at` DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `updated_at` DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `version` INT UNSIGNED NOT NULL DEFAULT 1,
  `tenant_id` CHAR(36) NOT NULL,
  `task_id` CHAR(36) NOT NULL,
  `evidence_id` CHAR(36) NOT NULL,
  `image_id` CHAR(36) NOT NULL,
  `ordinal` INT UNSIGNED NOT NULL DEFAULT 0,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_evidence_images_0` (`tenant_id`, `id`),
  UNIQUE KEY `uq_evidence_images_1` (`tenant_id`, `evidence_id`, `image_id`),
  UNIQUE KEY `uq_evidence_images_2` (`tenant_id`, `evidence_id`, `ordinal`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE `run_images` (
  `id` CHAR(36) NOT NULL,
  `created_at` DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `updated_at` DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `version` INT UNSIGNED NOT NULL DEFAULT 1,
  `tenant_id` CHAR(36) NOT NULL,
  `item_id` CHAR(36) NOT NULL,
  `run_id` CHAR(36) NOT NULL,
  `image_id` CHAR(36) NOT NULL,
  `ordinal` INT UNSIGNED NOT NULL DEFAULT 0,
  `role` ENUM('overview','detail') NOT NULL DEFAULT 'overview',
  `parent_image_id` CHAR(36) NULL,
  `analysis_sha256` CHAR(64) NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_run_images_0` (`tenant_id`, `id`),
  UNIQUE KEY `uq_run_images_1` (`tenant_id`, `run_id`, `image_id`),
  UNIQUE KEY `uq_run_images_2` (`tenant_id`, `run_id`, `ordinal`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE `image_derivatives` (
  `id` CHAR(36) NOT NULL,
  `created_at` DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `updated_at` DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `version` INT UNSIGNED NOT NULL DEFAULT 1,
  `tenant_id` CHAR(36) NOT NULL,
  `run_id` CHAR(36) NOT NULL,
  `image_id` CHAR(36) NOT NULL,
  `crop_id` CHAR(36) NOT NULL,
  `detection_id` CHAR(36) NOT NULL,
  `kind` ENUM('crop') NOT NULL DEFAULT 'crop',
  `recipe` JSON NOT NULL,
  `object_key` VARCHAR(1024) NOT NULL,
  `object_version` VARCHAR(200) NOT NULL,
  `sha256` CHAR(64) NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_image_derivatives_0` (`tenant_id`, `id`),
  UNIQUE KEY `uq_image_derivatives_1` (`tenant_id`, `run_id`, `crop_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE `api_idempotency` (
  `id` CHAR(36) NOT NULL,
  `created_at` DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `updated_at` DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `version` INT UNSIGNED NOT NULL DEFAULT 1,
  `tenant_id` CHAR(36) NOT NULL,
  `actor_id` CHAR(36) NOT NULL,
  `method` VARCHAR(10) NOT NULL,
  `path_hash` CHAR(64) NOT NULL,
  `key_hash` CHAR(64) NOT NULL,
  `request_hash` CHAR(64) NOT NULL,
  `state` ENUM('pending','completed') NOT NULL DEFAULT 'pending',
  `response_status` SMALLINT UNSIGNED NULL,
  `response` JSON NULL,
  `lease_owner` CHAR(36) NULL,
  `lease_until` DATETIME(3) NULL,
  `expires_at` DATETIME(3) NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_api_idempotency_0` (`tenant_id`, `id`),
  UNIQUE KEY `uq_api_idempotency_1` (`tenant_id`, `actor_id`, `method`, `path_hash`, `key_hash`),
  KEY `ix_api_idempotency_0` (`expires_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE `outbox_events` (
  `id` CHAR(36) NOT NULL,
  `created_at` DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `updated_at` DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `version` INT UNSIGNED NOT NULL DEFAULT 1,
  `tenant_id` CHAR(36) NOT NULL,
  `event_type` VARCHAR(80) NOT NULL,
  `aggregate_type` VARCHAR(80) NOT NULL,
  `aggregate_id` CHAR(36) NOT NULL,
  `aggregate_version` INT UNSIGNED NOT NULL DEFAULT 1,
  `payload` JSON NOT NULL,
  `state` ENUM('pending','leased','published') NOT NULL DEFAULT 'pending',
  `lease_owner` CHAR(36) NULL,
  `lease_until` DATETIME(3) NULL,
  `attempts` INT UNSIGNED NOT NULL DEFAULT 0,
  `available_at` DATETIME(3) NOT NULL,
  `published_at` DATETIME(3) NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_outbox_events_0` (`tenant_id`, `id`),
  KEY `ix_outbox_events_0` (`state`, `available_at`, `id`),
  KEY `ix_outbox_events_1` (`state`, `lease_until`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE `event_inbox` (
  `id` CHAR(36) NOT NULL,
  `created_at` DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `updated_at` DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `version` INT UNSIGNED NOT NULL DEFAULT 1,
  `tenant_id` CHAR(36) NOT NULL,
  `event_id` CHAR(36) NOT NULL,
  `consumer` VARCHAR(80) NOT NULL,
  `processed_at` DATETIME(3) NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_event_inbox_0` (`tenant_id`, `id`),
  UNIQUE KEY `uq_event_inbox_1` (`tenant_id`, `event_id`, `consumer`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE `task_runs` (
  `id` CHAR(36) NOT NULL,
  `created_at` DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `updated_at` DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `version` INT UNSIGNED NOT NULL DEFAULT 1,
  `tenant_id` CHAR(36) NOT NULL,
  `task_type` VARCHAR(80) NOT NULL,
  `resource_id` CHAR(36) NOT NULL,
  `logical_key` VARCHAR(200) NOT NULL,
  `payload` JSON NOT NULL,
  `state` ENUM('ready','leased','retry_wait','succeeded','failed','dead_letter') NOT NULL DEFAULT 'ready',
  `attempt` INT UNSIGNED NOT NULL DEFAULT 0,
  `replay_generation` INT UNSIGNED NOT NULL DEFAULT 0,
  `dispatch_sequence` INT UNSIGNED NOT NULL DEFAULT 1,
  `lease_owner` CHAR(36) NULL,
  `lease_until` DATETIME(3) NULL,
  `fencing_token` BIGINT UNSIGNED NOT NULL DEFAULT 0,
  `heartbeat_at` DATETIME(3) NULL,
  `available_at` DATETIME(3) NOT NULL,
  `last_dispatched_at` DATETIME(3) NULL,
  `last_error_code` VARCHAR(80) NULL,
  `started_at` DATETIME(3) NULL,
  `finished_at` DATETIME(3) NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_task_runs_0` (`tenant_id`, `id`),
  UNIQUE KEY `uq_task_runs_1` (`tenant_id`, `logical_key`),
  KEY `ix_task_runs_0` (`state`, `available_at`, `id`),
  KEY `ix_task_runs_1` (`state`, `lease_until`),
  CONSTRAINT `ck_task_runs_0` CHECK (attempt <= 4)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE `task_attempts` (
  `id` CHAR(36) NOT NULL,
  `created_at` DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `updated_at` DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `version` INT UNSIGNED NOT NULL DEFAULT 1,
  `tenant_id` CHAR(36) NOT NULL,
  `task_id` CHAR(36) NOT NULL,
  `replay_generation` INT UNSIGNED NOT NULL DEFAULT 0,
  `attempt` INT UNSIGNED NOT NULL DEFAULT 1,
  `fencing_token` BIGINT UNSIGNED NOT NULL,
  `lease_owner` CHAR(36) NOT NULL,
  `started_at` DATETIME(3) NOT NULL,
  `finished_at` DATETIME(3) NULL,
  `status` ENUM('running','succeeded','failed','abandoned') NOT NULL DEFAULT 'running',
  `error_code` VARCHAR(80) NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_task_attempts_0` (`tenant_id`, `id`),
  UNIQUE KEY `uq_task_attempts_1` (`tenant_id`, `task_id`, `replay_generation`, `attempt`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE `notifications` (
  `id` CHAR(36) NOT NULL,
  `created_at` DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `updated_at` DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `version` INT UNSIGNED NOT NULL DEFAULT 1,
  `tenant_id` CHAR(36) NOT NULL,
  `event_id` CHAR(36) NOT NULL,
  `recipient_id` CHAR(36) NOT NULL,
  `type` VARCHAR(80) NOT NULL,
  `resource_type` VARCHAR(80) NOT NULL,
  `resource_id` CHAR(36) NOT NULL,
  `read_at` DATETIME(3) NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_notifications_0` (`tenant_id`, `id`),
  UNIQUE KEY `uq_notifications_1` (`tenant_id`, `event_id`, `recipient_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE `audit_events` (
  `id` CHAR(36) NOT NULL,
  `created_at` DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `updated_at` DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `version` INT UNSIGNED NOT NULL DEFAULT 1,
  `tenant_id` CHAR(36) NOT NULL,
  `actor_id` CHAR(36) NULL,
  `action` VARCHAR(100) NOT NULL,
  `resource_type` VARCHAR(80) NOT NULL,
  `resource_id` CHAR(36) NOT NULL,
  `changes` JSON NOT NULL,
  `reason` VARCHAR(2000) NOT NULL,
  `request_id` CHAR(36) NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_audit_events_0` (`tenant_id`, `id`),
  KEY `ix_audit_events_0` (`tenant_id`, `resource_type`, `resource_id`, `created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE `report_exports` (
  `id` CHAR(36) NOT NULL,
  `created_at` DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `updated_at` DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `version` INT UNSIGNED NOT NULL DEFAULT 1,
  `tenant_id` CHAR(36) NOT NULL,
  `requested_by` CHAR(36) NOT NULL,
  `format` ENUM('csv','pdf') NOT NULL DEFAULT 'csv',
  `filters` JSON NOT NULL,
  `snapshot_at` DATETIME(3) NOT NULL,
  `snapshot` JSON NOT NULL,
  `status` ENUM('queued','running','ready','failed','expired') NOT NULL DEFAULT 'queued',
  `object_key` VARCHAR(1024) NULL,
  `checksum` CHAR(64) NULL,
  `expires_at` DATETIME(3) NULL,
  `last_error_code` VARCHAR(80) NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_report_exports_0` (`tenant_id`, `id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE `dataset_versions` (
  `id` CHAR(36) NOT NULL,
  `created_at` DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `updated_at` DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `version` INT UNSIGNED NOT NULL DEFAULT 1,
  `tenant_id` CHAR(36) NOT NULL,
  `name` VARCHAR(100) NOT NULL,
  `checksum` CHAR(64) NOT NULL,
  `manifest_key` VARCHAR(1024) NOT NULL,
  `authorization_ref` VARCHAR(1000) NOT NULL,
  `status` ENUM('draft','frozen','retired') NOT NULL DEFAULT 'draft',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_dataset_versions_0` (`tenant_id`, `id`),
  UNIQUE KEY `uq_dataset_versions_1` (`tenant_id`, `name`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE `dataset_images` (
  `id` CHAR(36) NOT NULL,
  `created_at` DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `updated_at` DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `version` INT UNSIGNED NOT NULL DEFAULT 1,
  `tenant_id` CHAR(36) NOT NULL,
  `dataset_id` CHAR(36) NOT NULL,
  `image_id` CHAR(36) NOT NULL,
  `scene_id` VARCHAR(100) NOT NULL,
  `split` ENUM('train','validation','test') NOT NULL DEFAULT 'train',
  `annotation_key` VARCHAR(1024) NOT NULL,
  `provenance` VARCHAR(2000) NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_dataset_images_0` (`tenant_id`, `id`),
  UNIQUE KEY `uq_dataset_images_1` (`tenant_id`, `dataset_id`, `image_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

ALTER TABLE `colleges` ADD CONSTRAINT `fk_colleges_0` FOREIGN KEY (`tenant_id`) REFERENCES `tenants` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT;

ALTER TABLE `laboratories` ADD CONSTRAINT `fk_laboratories_0` FOREIGN KEY (`tenant_id`) REFERENCES `tenants` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT;

ALTER TABLE `laboratories` ADD CONSTRAINT `fk_laboratories_1` FOREIGN KEY (`tenant_id`, `college_id`) REFERENCES `colleges` (`tenant_id`, `id`) ON DELETE RESTRICT ON UPDATE RESTRICT;

ALTER TABLE `users` ADD CONSTRAINT `fk_users_0` FOREIGN KEY (`tenant_id`) REFERENCES `tenants` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT;

ALTER TABLE `user_roles` ADD CONSTRAINT `fk_user_roles_0` FOREIGN KEY (`tenant_id`) REFERENCES `tenants` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT;

ALTER TABLE `user_roles` ADD CONSTRAINT `fk_user_roles_1` FOREIGN KEY (`tenant_id`, `user_id`) REFERENCES `users` (`tenant_id`, `id`) ON DELETE RESTRICT ON UPDATE RESTRICT;

ALTER TABLE `user_roles` ADD CONSTRAINT `fk_user_roles_2` FOREIGN KEY (`tenant_id`, `laboratory_id`) REFERENCES `laboratories` (`tenant_id`, `id`) ON DELETE RESTRICT ON UPDATE RESTRICT;

ALTER TABLE `user_roles` ADD CONSTRAINT `fk_user_roles_3` FOREIGN KEY (`role_id`) REFERENCES `roles` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT;

ALTER TABLE `sessions` ADD CONSTRAINT `fk_sessions_0` FOREIGN KEY (`tenant_id`) REFERENCES `tenants` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT;

ALTER TABLE `sessions` ADD CONSTRAINT `fk_sessions_1` FOREIGN KEY (`tenant_id`, `user_id`) REFERENCES `users` (`tenant_id`, `id`) ON DELETE RESTRICT ON UPDATE RESTRICT;

ALTER TABLE `locations` ADD CONSTRAINT `fk_locations_0` FOREIGN KEY (`tenant_id`) REFERENCES `tenants` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT;

ALTER TABLE `locations` ADD CONSTRAINT `fk_locations_1` FOREIGN KEY (`tenant_id`, `laboratory_id`) REFERENCES `laboratories` (`tenant_id`, `id`) ON DELETE RESTRICT ON UPDATE RESTRICT;

ALTER TABLE `locations` ADD CONSTRAINT `fk_locations_2` FOREIGN KEY (`tenant_id`, `laboratory_id`, `parent_id`) REFERENCES `locations` (`tenant_id`, `laboratory_id`, `id`) ON DELETE RESTRICT ON UPDATE RESTRICT;

ALTER TABLE `inspection_templates` ADD CONSTRAINT `fk_inspection_templates_0` FOREIGN KEY (`tenant_id`) REFERENCES `tenants` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT;

ALTER TABLE `template_items` ADD CONSTRAINT `fk_template_items_0` FOREIGN KEY (`tenant_id`) REFERENCES `tenants` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT;

ALTER TABLE `template_items` ADD CONSTRAINT `fk_template_items_1` FOREIGN KEY (`tenant_id`, `template_id`) REFERENCES `inspection_templates` (`tenant_id`, `id`) ON DELETE RESTRICT ON UPDATE RESTRICT;

ALTER TABLE `inspections` ADD CONSTRAINT `fk_inspections_0` FOREIGN KEY (`tenant_id`) REFERENCES `tenants` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT;

ALTER TABLE `inspections` ADD CONSTRAINT `fk_inspections_1` FOREIGN KEY (`tenant_id`, `laboratory_id`) REFERENCES `laboratories` (`tenant_id`, `id`) ON DELETE RESTRICT ON UPDATE RESTRICT;

ALTER TABLE `inspections` ADD CONSTRAINT `fk_inspections_2` FOREIGN KEY (`tenant_id`, `template_id`) REFERENCES `inspection_templates` (`tenant_id`, `id`) ON DELETE RESTRICT ON UPDATE RESTRICT;

ALTER TABLE `inspections` ADD CONSTRAINT `fk_inspections_3` FOREIGN KEY (`tenant_id`, `inspector_id`) REFERENCES `users` (`tenant_id`, `id`) ON DELETE RESTRICT ON UPDATE RESTRICT;

ALTER TABLE `inspection_items` ADD CONSTRAINT `fk_inspection_items_0` FOREIGN KEY (`tenant_id`) REFERENCES `tenants` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT;

ALTER TABLE `inspection_items` ADD CONSTRAINT `fk_inspection_items_1` FOREIGN KEY (`tenant_id`, `laboratory_id`, `inspection_id`) REFERENCES `inspections` (`tenant_id`, `laboratory_id`, `id`) ON DELETE RESTRICT ON UPDATE RESTRICT;

ALTER TABLE `inspection_items` ADD CONSTRAINT `fk_inspection_items_2` FOREIGN KEY (`tenant_id`, `template_item_id`) REFERENCES `template_items` (`tenant_id`, `id`) ON DELETE RESTRICT ON UPDATE RESTRICT;

ALTER TABLE `inspection_items` ADD CONSTRAINT `fk_inspection_items_3` FOREIGN KEY (`tenant_id`, `laboratory_id`, `location_id`) REFERENCES `locations` (`tenant_id`, `laboratory_id`, `id`) ON DELETE RESTRICT ON UPDATE RESTRICT;

ALTER TABLE `inspection_items` ADD CONSTRAINT `fk_inspection_items_4` FOREIGN KEY (`tenant_id`, `reviewed_by`) REFERENCES `users` (`tenant_id`, `id`) ON DELETE RESTRICT ON UPDATE RESTRICT;

ALTER TABLE `inspection_items` ADD CONSTRAINT `fk_inspection_items_5` FOREIGN KEY (`tenant_id`, `id`, `current_run_id`) REFERENCES `inference_runs` (`tenant_id`, `item_id`, `id`) ON DELETE RESTRICT ON UPDATE RESTRICT;

ALTER TABLE `inspection_items` ADD CONSTRAINT `fk_inspection_items_6` FOREIGN KEY (`tenant_id`, `id`, `current_fact_revision_id`) REFERENCES `fact_revisions` (`tenant_id`, `item_id`, `id`) ON DELETE RESTRICT ON UPDATE RESTRICT;

ALTER TABLE `inspection_items` ADD CONSTRAINT `fk_inspection_items_7` FOREIGN KEY (`tenant_id`, `id`, `current_evaluation_id`) REFERENCES `rule_evaluations` (`tenant_id`, `item_id`, `id`) ON DELETE RESTRICT ON UPDATE RESTRICT;

ALTER TABLE `dictionary_versions` ADD CONSTRAINT `fk_dictionary_versions_0` FOREIGN KEY (`tenant_id`) REFERENCES `tenants` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT;

ALTER TABLE `chemical_entities` ADD CONSTRAINT `fk_chemical_entities_0` FOREIGN KEY (`tenant_id`) REFERENCES `tenants` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT;

ALTER TABLE `chemical_entities` ADD CONSTRAINT `fk_chemical_entities_1` FOREIGN KEY (`tenant_id`, `dictionary_version_id`) REFERENCES `dictionary_versions` (`tenant_id`, `id`) ON DELETE RESTRICT ON UPDATE RESTRICT;

ALTER TABLE `chemical_aliases` ADD CONSTRAINT `fk_chemical_aliases_0` FOREIGN KEY (`tenant_id`) REFERENCES `tenants` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT;

ALTER TABLE `chemical_aliases` ADD CONSTRAINT `fk_chemical_aliases_1` FOREIGN KEY (`tenant_id`, `dictionary_version_id`, `entity_id`) REFERENCES `chemical_entities` (`tenant_id`, `dictionary_version_id`, `id`) ON DELETE RESTRICT ON UPDATE RESTRICT;

ALTER TABLE `model_versions` ADD CONSTRAINT `fk_model_versions_0` FOREIGN KEY (`tenant_id`) REFERENCES `tenants` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT;

ALTER TABLE `model_versions` ADD CONSTRAINT `fk_model_versions_1` FOREIGN KEY (`tenant_id`, `dictionary_version_id`) REFERENCES `dictionary_versions` (`tenant_id`, `id`) ON DELETE RESTRICT ON UPDATE RESTRICT;

ALTER TABLE `rule_sets` ADD CONSTRAINT `fk_rule_sets_0` FOREIGN KEY (`tenant_id`) REFERENCES `tenants` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT;

ALTER TABLE `rule_versions` ADD CONSTRAINT `fk_rule_versions_0` FOREIGN KEY (`tenant_id`) REFERENCES `tenants` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT;

ALTER TABLE `rule_versions` ADD CONSTRAINT `fk_rule_versions_1` FOREIGN KEY (`tenant_id`, `rule_set_id`) REFERENCES `rule_sets` (`tenant_id`, `id`) ON DELETE RESTRICT ON UPDATE RESTRICT;

ALTER TABLE `rule_versions` ADD CONSTRAINT `fk_rule_versions_2` FOREIGN KEY (`tenant_id`, `submitted_by`) REFERENCES `users` (`tenant_id`, `id`) ON DELETE RESTRICT ON UPDATE RESTRICT;

ALTER TABLE `rule_versions` ADD CONSTRAINT `fk_rule_versions_3` FOREIGN KEY (`tenant_id`, `approved_by`) REFERENCES `users` (`tenant_id`, `id`) ON DELETE RESTRICT ON UPDATE RESTRICT;

ALTER TABLE `rule_bundles` ADD CONSTRAINT `fk_rule_bundles_0` FOREIGN KEY (`tenant_id`) REFERENCES `tenants` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT;

ALTER TABLE `rule_bundle_members` ADD CONSTRAINT `fk_rule_bundle_members_0` FOREIGN KEY (`tenant_id`) REFERENCES `tenants` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT;

ALTER TABLE `rule_bundle_members` ADD CONSTRAINT `fk_rule_bundle_members_1` FOREIGN KEY (`tenant_id`, `bundle_id`) REFERENCES `rule_bundles` (`tenant_id`, `id`) ON DELETE RESTRICT ON UPDATE RESTRICT;

ALTER TABLE `rule_bundle_members` ADD CONSTRAINT `fk_rule_bundle_members_2` FOREIGN KEY (`tenant_id`, `rule_version_id`) REFERENCES `rule_versions` (`tenant_id`, `id`) ON DELETE RESTRICT ON UPDATE RESTRICT;

ALTER TABLE `activations` ADD CONSTRAINT `fk_activations_0` FOREIGN KEY (`tenant_id`) REFERENCES `tenants` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT;

ALTER TABLE `activations` ADD CONSTRAINT `fk_activations_1` FOREIGN KEY (`tenant_id`, `laboratory_id`) REFERENCES `laboratories` (`tenant_id`, `id`) ON DELETE RESTRICT ON UPDATE RESTRICT;

ALTER TABLE `activations` ADD CONSTRAINT `fk_activations_2` FOREIGN KEY (`tenant_id`, `model_bundle_id`) REFERENCES `model_versions` (`tenant_id`, `id`) ON DELETE RESTRICT ON UPDATE RESTRICT;

ALTER TABLE `activations` ADD CONSTRAINT `fk_activations_3` FOREIGN KEY (`tenant_id`, `dictionary_version_id`) REFERENCES `dictionary_versions` (`tenant_id`, `id`) ON DELETE RESTRICT ON UPDATE RESTRICT;

ALTER TABLE `activations` ADD CONSTRAINT `fk_activations_4` FOREIGN KEY (`tenant_id`, `rule_bundle_id`) REFERENCES `rule_bundles` (`tenant_id`, `id`) ON DELETE RESTRICT ON UPDATE RESTRICT;

ALTER TABLE `inference_runs` ADD CONSTRAINT `fk_inference_runs_0` FOREIGN KEY (`tenant_id`) REFERENCES `tenants` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT;

ALTER TABLE `inference_runs` ADD CONSTRAINT `fk_inference_runs_1` FOREIGN KEY (`tenant_id`, `laboratory_id`, `item_id`) REFERENCES `inspection_items` (`tenant_id`, `laboratory_id`, `id`) ON DELETE RESTRICT ON UPDATE RESTRICT;

ALTER TABLE `inference_runs` ADD CONSTRAINT `fk_inference_runs_2` FOREIGN KEY (`tenant_id`, `replay_of`) REFERENCES `inference_runs` (`tenant_id`, `id`) ON DELETE RESTRICT ON UPDATE RESTRICT;

ALTER TABLE `inference_runs` ADD CONSTRAINT `fk_inference_runs_3` FOREIGN KEY (`tenant_id`, `model_bundle_id`) REFERENCES `model_versions` (`tenant_id`, `id`) ON DELETE RESTRICT ON UPDATE RESTRICT;

ALTER TABLE `inference_runs` ADD CONSTRAINT `fk_inference_runs_4` FOREIGN KEY (`tenant_id`, `dictionary_version_id`) REFERENCES `dictionary_versions` (`tenant_id`, `id`) ON DELETE RESTRICT ON UPDATE RESTRICT;

ALTER TABLE `inference_runs` ADD CONSTRAINT `fk_inference_runs_5` FOREIGN KEY (`tenant_id`, `rule_bundle_id`) REFERENCES `rule_bundles` (`tenant_id`, `id`) ON DELETE RESTRICT ON UPDATE RESTRICT;

ALTER TABLE `fact_revisions` ADD CONSTRAINT `fk_fact_revisions_0` FOREIGN KEY (`tenant_id`) REFERENCES `tenants` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT;

ALTER TABLE `fact_revisions` ADD CONSTRAINT `fk_fact_revisions_1` FOREIGN KEY (`tenant_id`, `item_id`, `run_id`) REFERENCES `inference_runs` (`tenant_id`, `item_id`, `id`) ON DELETE RESTRICT ON UPDATE RESTRICT;

ALTER TABLE `fact_revisions` ADD CONSTRAINT `fk_fact_revisions_2` FOREIGN KEY (`tenant_id`, `created_by`) REFERENCES `users` (`tenant_id`, `id`) ON DELETE RESTRICT ON UPDATE RESTRICT;

ALTER TABLE `rule_evaluations` ADD CONSTRAINT `fk_rule_evaluations_0` FOREIGN KEY (`tenant_id`) REFERENCES `tenants` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT;

ALTER TABLE `rule_evaluations` ADD CONSTRAINT `fk_rule_evaluations_1` FOREIGN KEY (`tenant_id`, `item_id`, `run_id`) REFERENCES `inference_runs` (`tenant_id`, `item_id`, `id`) ON DELETE RESTRICT ON UPDATE RESTRICT;

ALTER TABLE `rule_evaluations` ADD CONSTRAINT `fk_rule_evaluations_2` FOREIGN KEY (`tenant_id`, `item_id`, `fact_revision_id`) REFERENCES `fact_revisions` (`tenant_id`, `item_id`, `id`) ON DELETE RESTRICT ON UPDATE RESTRICT;

ALTER TABLE `rule_evaluations` ADD CONSTRAINT `fk_rule_evaluations_3` FOREIGN KEY (`tenant_id`, `rule_bundle_id`) REFERENCES `rule_bundles` (`tenant_id`, `id`) ON DELETE RESTRICT ON UPDATE RESTRICT;

ALTER TABLE `findings` ADD CONSTRAINT `fk_findings_0` FOREIGN KEY (`tenant_id`) REFERENCES `tenants` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT;

ALTER TABLE `findings` ADD CONSTRAINT `fk_findings_1` FOREIGN KEY (`tenant_id`, `item_id`, `run_id`) REFERENCES `inference_runs` (`tenant_id`, `item_id`, `id`) ON DELETE RESTRICT ON UPDATE RESTRICT;

ALTER TABLE `findings` ADD CONSTRAINT `fk_findings_2` FOREIGN KEY (`tenant_id`, `item_id`, `fact_revision_id`) REFERENCES `fact_revisions` (`tenant_id`, `item_id`, `id`) ON DELETE RESTRICT ON UPDATE RESTRICT;

ALTER TABLE `findings` ADD CONSTRAINT `fk_findings_3` FOREIGN KEY (`tenant_id`, `item_id`, `rule_evaluation_id`) REFERENCES `rule_evaluations` (`tenant_id`, `item_id`, `id`) ON DELETE RESTRICT ON UPDATE RESTRICT;

ALTER TABLE `findings` ADD CONSTRAINT `fk_findings_4` FOREIGN KEY (`tenant_id`, `confirmed_by`) REFERENCES `users` (`tenant_id`, `id`) ON DELETE RESTRICT ON UPDATE RESTRICT;

ALTER TABLE `review_actions` ADD CONSTRAINT `fk_review_actions_0` FOREIGN KEY (`tenant_id`) REFERENCES `tenants` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT;

ALTER TABLE `review_actions` ADD CONSTRAINT `fk_review_actions_1` FOREIGN KEY (`tenant_id`, `actor_id`) REFERENCES `users` (`tenant_id`, `id`) ON DELETE RESTRICT ON UPDATE RESTRICT;

ALTER TABLE `remediation_tasks` ADD CONSTRAINT `fk_remediation_tasks_0` FOREIGN KEY (`tenant_id`) REFERENCES `tenants` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT;

ALTER TABLE `remediation_tasks` ADD CONSTRAINT `fk_remediation_tasks_1` FOREIGN KEY (`tenant_id`, `finding_id`) REFERENCES `findings` (`tenant_id`, `id`) ON DELETE RESTRICT ON UPDATE RESTRICT;

ALTER TABLE `remediation_tasks` ADD CONSTRAINT `fk_remediation_tasks_2` FOREIGN KEY (`tenant_id`, `laboratory_id`) REFERENCES `laboratories` (`tenant_id`, `id`) ON DELETE RESTRICT ON UPDATE RESTRICT;

ALTER TABLE `remediation_tasks` ADD CONSTRAINT `fk_remediation_tasks_3` FOREIGN KEY (`tenant_id`, `assignee_id`) REFERENCES `users` (`tenant_id`, `id`) ON DELETE RESTRICT ON UPDATE RESTRICT;

ALTER TABLE `remediation_tasks` ADD CONSTRAINT `fk_remediation_tasks_4` FOREIGN KEY (`tenant_id`, `id`, `latest_evidence_id`) REFERENCES `remediation_evidence` (`tenant_id`, `task_id`, `id`) ON DELETE RESTRICT ON UPDATE RESTRICT;

ALTER TABLE `remediation_evidence` ADD CONSTRAINT `fk_remediation_evidence_0` FOREIGN KEY (`tenant_id`) REFERENCES `tenants` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT;

ALTER TABLE `remediation_evidence` ADD CONSTRAINT `fk_remediation_evidence_1` FOREIGN KEY (`tenant_id`, `task_id`) REFERENCES `remediation_tasks` (`tenant_id`, `id`) ON DELETE RESTRICT ON UPDATE RESTRICT;

ALTER TABLE `remediation_evidence` ADD CONSTRAINT `fk_remediation_evidence_2` FOREIGN KEY (`tenant_id`, `submitted_by`) REFERENCES `users` (`tenant_id`, `id`) ON DELETE RESTRICT ON UPDATE RESTRICT;

ALTER TABLE `uploads` ADD CONSTRAINT `fk_uploads_0` FOREIGN KEY (`tenant_id`) REFERENCES `tenants` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT;

ALTER TABLE `uploads` ADD CONSTRAINT `fk_uploads_1` FOREIGN KEY (`tenant_id`, `requested_by`) REFERENCES `users` (`tenant_id`, `id`) ON DELETE RESTRICT ON UPDATE RESTRICT;

ALTER TABLE `uploads` ADD CONSTRAINT `fk_uploads_2` FOREIGN KEY (`tenant_id`, `laboratory_id`) REFERENCES `laboratories` (`tenant_id`, `id`) ON DELETE RESTRICT ON UPDATE RESTRICT;

ALTER TABLE `uploads` ADD CONSTRAINT `fk_uploads_3` FOREIGN KEY (`tenant_id`, `laboratory_id`, `inspection_item_id`) REFERENCES `inspection_items` (`tenant_id`, `laboratory_id`, `id`) ON DELETE RESTRICT ON UPDATE RESTRICT;

ALTER TABLE `uploads` ADD CONSTRAINT `fk_uploads_4` FOREIGN KEY (`tenant_id`, `laboratory_id`, `remediation_task_id`) REFERENCES `remediation_tasks` (`tenant_id`, `laboratory_id`, `id`) ON DELETE RESTRICT ON UPDATE RESTRICT;

ALTER TABLE `asset_images` ADD CONSTRAINT `fk_asset_images_0` FOREIGN KEY (`tenant_id`) REFERENCES `tenants` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT;

ALTER TABLE `asset_images` ADD CONSTRAINT `fk_asset_images_1` FOREIGN KEY (`tenant_id`, `upload_id`) REFERENCES `uploads` (`tenant_id`, `id`) ON DELETE RESTRICT ON UPDATE RESTRICT;

ALTER TABLE `asset_images` ADD CONSTRAINT `fk_asset_images_2` FOREIGN KEY (`tenant_id`, `laboratory_id`) REFERENCES `laboratories` (`tenant_id`, `id`) ON DELETE RESTRICT ON UPDATE RESTRICT;

ALTER TABLE `asset_images` ADD CONSTRAINT `fk_asset_images_3` FOREIGN KEY (`tenant_id`, `laboratory_id`, `inspection_item_id`) REFERENCES `inspection_items` (`tenant_id`, `laboratory_id`, `id`) ON DELETE RESTRICT ON UPDATE RESTRICT;

ALTER TABLE `asset_images` ADD CONSTRAINT `fk_asset_images_4` FOREIGN KEY (`tenant_id`, `laboratory_id`, `remediation_task_id`) REFERENCES `remediation_tasks` (`tenant_id`, `laboratory_id`, `id`) ON DELETE RESTRICT ON UPDATE RESTRICT;

ALTER TABLE `evidence_images` ADD CONSTRAINT `fk_evidence_images_0` FOREIGN KEY (`tenant_id`) REFERENCES `tenants` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT;

ALTER TABLE `evidence_images` ADD CONSTRAINT `fk_evidence_images_1` FOREIGN KEY (`tenant_id`, `task_id`, `evidence_id`) REFERENCES `remediation_evidence` (`tenant_id`, `task_id`, `id`) ON DELETE RESTRICT ON UPDATE RESTRICT;

ALTER TABLE `evidence_images` ADD CONSTRAINT `fk_evidence_images_2` FOREIGN KEY (`tenant_id`, `task_id`, `image_id`) REFERENCES `asset_images` (`tenant_id`, `remediation_task_id`, `id`) ON DELETE RESTRICT ON UPDATE RESTRICT;

ALTER TABLE `run_images` ADD CONSTRAINT `fk_run_images_0` FOREIGN KEY (`tenant_id`) REFERENCES `tenants` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT;

ALTER TABLE `run_images` ADD CONSTRAINT `fk_run_images_1` FOREIGN KEY (`tenant_id`, `item_id`, `run_id`) REFERENCES `inference_runs` (`tenant_id`, `item_id`, `id`) ON DELETE RESTRICT ON UPDATE RESTRICT;

ALTER TABLE `run_images` ADD CONSTRAINT `fk_run_images_2` FOREIGN KEY (`tenant_id`, `item_id`, `image_id`) REFERENCES `asset_images` (`tenant_id`, `inspection_item_id`, `id`) ON DELETE RESTRICT ON UPDATE RESTRICT;

ALTER TABLE `run_images` ADD CONSTRAINT `fk_run_images_3` FOREIGN KEY (`tenant_id`, `run_id`, `parent_image_id`) REFERENCES `run_images` (`tenant_id`, `run_id`, `image_id`) ON DELETE RESTRICT ON UPDATE RESTRICT;

ALTER TABLE `image_derivatives` ADD CONSTRAINT `fk_image_derivatives_0` FOREIGN KEY (`tenant_id`) REFERENCES `tenants` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT;

ALTER TABLE `image_derivatives` ADD CONSTRAINT `fk_image_derivatives_1` FOREIGN KEY (`tenant_id`, `run_id`, `image_id`) REFERENCES `run_images` (`tenant_id`, `run_id`, `image_id`) ON DELETE RESTRICT ON UPDATE RESTRICT;

ALTER TABLE `api_idempotency` ADD CONSTRAINT `fk_api_idempotency_0` FOREIGN KEY (`tenant_id`) REFERENCES `tenants` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT;

ALTER TABLE `api_idempotency` ADD CONSTRAINT `fk_api_idempotency_1` FOREIGN KEY (`tenant_id`, `actor_id`) REFERENCES `users` (`tenant_id`, `id`) ON DELETE RESTRICT ON UPDATE RESTRICT;

ALTER TABLE `outbox_events` ADD CONSTRAINT `fk_outbox_events_0` FOREIGN KEY (`tenant_id`) REFERENCES `tenants` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT;

ALTER TABLE `event_inbox` ADD CONSTRAINT `fk_event_inbox_0` FOREIGN KEY (`tenant_id`) REFERENCES `tenants` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT;

ALTER TABLE `event_inbox` ADD CONSTRAINT `fk_event_inbox_1` FOREIGN KEY (`tenant_id`, `event_id`) REFERENCES `outbox_events` (`tenant_id`, `id`) ON DELETE RESTRICT ON UPDATE RESTRICT;

ALTER TABLE `task_runs` ADD CONSTRAINT `fk_task_runs_0` FOREIGN KEY (`tenant_id`) REFERENCES `tenants` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT;

ALTER TABLE `task_attempts` ADD CONSTRAINT `fk_task_attempts_0` FOREIGN KEY (`tenant_id`) REFERENCES `tenants` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT;

ALTER TABLE `task_attempts` ADD CONSTRAINT `fk_task_attempts_1` FOREIGN KEY (`tenant_id`, `task_id`) REFERENCES `task_runs` (`tenant_id`, `id`) ON DELETE RESTRICT ON UPDATE RESTRICT;

ALTER TABLE `notifications` ADD CONSTRAINT `fk_notifications_0` FOREIGN KEY (`tenant_id`) REFERENCES `tenants` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT;

ALTER TABLE `notifications` ADD CONSTRAINT `fk_notifications_1` FOREIGN KEY (`tenant_id`, `event_id`) REFERENCES `outbox_events` (`tenant_id`, `id`) ON DELETE RESTRICT ON UPDATE RESTRICT;

ALTER TABLE `notifications` ADD CONSTRAINT `fk_notifications_2` FOREIGN KEY (`tenant_id`, `recipient_id`) REFERENCES `users` (`tenant_id`, `id`) ON DELETE RESTRICT ON UPDATE RESTRICT;

ALTER TABLE `audit_events` ADD CONSTRAINT `fk_audit_events_0` FOREIGN KEY (`tenant_id`) REFERENCES `tenants` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT;

ALTER TABLE `audit_events` ADD CONSTRAINT `fk_audit_events_1` FOREIGN KEY (`tenant_id`, `actor_id`) REFERENCES `users` (`tenant_id`, `id`) ON DELETE RESTRICT ON UPDATE RESTRICT;

ALTER TABLE `report_exports` ADD CONSTRAINT `fk_report_exports_0` FOREIGN KEY (`tenant_id`) REFERENCES `tenants` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT;

ALTER TABLE `report_exports` ADD CONSTRAINT `fk_report_exports_1` FOREIGN KEY (`tenant_id`, `requested_by`) REFERENCES `users` (`tenant_id`, `id`) ON DELETE RESTRICT ON UPDATE RESTRICT;

ALTER TABLE `dataset_versions` ADD CONSTRAINT `fk_dataset_versions_0` FOREIGN KEY (`tenant_id`) REFERENCES `tenants` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT;

ALTER TABLE `dataset_images` ADD CONSTRAINT `fk_dataset_images_0` FOREIGN KEY (`tenant_id`) REFERENCES `tenants` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT;

ALTER TABLE `dataset_images` ADD CONSTRAINT `fk_dataset_images_1` FOREIGN KEY (`tenant_id`, `dataset_id`) REFERENCES `dataset_versions` (`tenant_id`, `id`) ON DELETE RESTRICT ON UPDATE RESTRICT;

ALTER TABLE `dataset_images` ADD CONSTRAINT `fk_dataset_images_2` FOREIGN KEY (`tenant_id`, `image_id`) REFERENCES `asset_images` (`tenant_id`, `id`) ON DELETE RESTRICT ON UPDATE RESTRICT;
