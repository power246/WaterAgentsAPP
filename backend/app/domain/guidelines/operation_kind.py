from app.domain.domain_enum import DomainEnum


class GuidelineOperationKind(DomainEnum):
    CREATE = "create"
    SAVE_DRAFT = "save_draft"
    LOCAL_EDIT = "local_edit"
    PUBLISH = "publish"
    SET_ENABLED = "set_enabled"
    RESTORE_BUNDLED = "restore_bundled"
    REBUILD_INDEX = "rebuild_index"
    RETRY = "retry"
