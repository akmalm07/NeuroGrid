import type { CollectionSchema } from 'deepspace/schema'

const readOnly = { read: true, create: false, update: false, delete: false } as const

export const gridStatusSchema: CollectionSchema = {
  name: 'grid_status',
  columns: [{ name: 'revision', storage: 'text', interpretation: 'plain' }],
  permissions: { anonymous: readOnly, viewer: readOnly, member: readOnly, admin: readOnly },
}
