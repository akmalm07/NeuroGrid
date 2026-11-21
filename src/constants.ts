/** App name — replaced by the CLI during scaffolding */
export const APP_NAME = 'neurogrid'
/** Immutable app identity — data scope keys to this, so renames never
 *  strand your records. */
export const APP_ID = 'app_01M7YEC6YWKJE9V3KWR0HJM00Y'

/** Primary scope ID for the app's RecordRoom DO */
export const SCOPE_ID = `app:${APP_ID}`

/** Roles and display config — imported from SDK (single source of truth) */
export { ROLES, ROLE_CONFIG, type Role } from 'deepspace'
