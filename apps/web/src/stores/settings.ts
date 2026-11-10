import { create } from 'zustand'
import {
  describeApiError,
  getModelSettings,
  updateModelSettings,
} from '../api/client'
import type { ModelSettings, ModelSettingsUpdate } from '../api/types'
import type { AsyncState } from './workspace'

interface SettingsState {
  open: boolean
  settings: AsyncState<ModelSettings>
  saving: boolean
  saveError: string | null
  savedAt: number | null
  setOpen: (open: boolean) => void
  refresh: () => Promise<void>
  /** Save a partial update; returns true on success. */
  save: (patch: ModelSettingsUpdate) => Promise<boolean>
}

export const useSettingsStore = create<SettingsState>()((set, get) => ({
  open: false,
  settings: { status: 'idle', error: null, data: null },
  saving: false,
  saveError: null,
  savedAt: null,

  setOpen: (open) => {
    set({ open, saveError: null })
    if (open) void get().refresh()
  },

  refresh: async () => {
    const prev = get().settings
    set({ settings: { status: 'loading', error: null, data: prev.data } })
    try {
      const data = await getModelSettings()
      set({ settings: { status: 'success', error: null, data } })
    } catch (err) {
      set({
        settings: { status: 'error', error: describeApiError(err), data: prev.data },
      })
    }
  },

  save: async (patch) => {
    set({ saving: true, saveError: null })
    try {
      const data = await updateModelSettings(patch)
      set({
        saving: false,
        settings: { status: 'success', error: null, data },
        savedAt: Date.now(),
      })
      return true
    } catch (err) {
      set({ saving: false, saveError: describeApiError(err) })
      return false
    }
  },
}))
