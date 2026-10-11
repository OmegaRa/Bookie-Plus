import { useState, useId } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Tag as TagIcon, Plus, X, ArrowRightLeft, Check, Trash2, Loader2 } from 'lucide-react'
import Dialog from './Dialog'
import * as api from '../api/client'
import { Tag } from '../types'

interface BulkTagDialogProps {
  open: boolean
  onClose: () => void
  selectedBookIds: number[]
}

type TabType = 'add' | 'remove' | 'replace'

export default function BulkTagDialog({ open, onClose, selectedBookIds }: BulkTagDialogProps) {
  const qc = useQueryClient()
  const [activeTab, setActiveTab] = useState<TabType>('add')
  const [statusMessage, setStatusMessage] = useState<{ text: string; type: 'success' | 'error' } | null>(null)
  const [isSubmitting, setIsSubmitting] = useState(false)

  // Staged additions
  const [stagedAddTags, setStagedAddTags] = useState<string[]>([])
  const [newTagInput, setNewTagInput] = useState('')

  // Staged removals
  const [tagsToRemove, setTagsToRemove] = useState<string[]>([])

  // Replace state
  const [oldTagToReplace, setOldTagToReplace] = useState('')
  const [newTagReplacement, setNewTagReplacement] = useState('')

  // Query all library tags
  const { data: allTags = [] } = useQuery<Tag[]>({
    queryKey: ['tags'],
    queryFn: () => api.getTags(),
    enabled: open,
  })

  // Query tag summary for currently selected books
  const { data: summary, isLoading: loadingSummary, refetch: refetchSummary } = useQuery<api.BulkTagsSummaryResponse>({
    queryKey: ['bulkTagsSummary', selectedBookIds],
    queryFn: () => api.bulkTagsSummary(selectedBookIds),
    enabled: open && selectedBookIds.length > 0,
  })

  const currentBookTags = summary?.tags ?? []
  const totalBooks = selectedBookIds.length

  const refreshAll = () => {
    qc.invalidateQueries({ queryKey: ['books'] })
    qc.invalidateQueries({ queryKey: ['tags'] })
    refetchSummary()
  }

  // --- Add Tab Handlers ---
  const handleStageAdd = (tag: string) => {
    const trimmed = tag.trim()
    if (!trimmed) return
    if (!stagedAddTags.includes(trimmed)) {
      setStagedAddTags(prev => [...prev, trimmed])
    }
    setNewTagInput('')
    setStatusMessage(null)
  }

  const handleRemoveStagedAdd = (tag: string) => {
    setStagedAddTags(prev => prev.filter(t => t !== tag))
  }

  const handleApplyAdd = async () => {
    if (stagedAddTags.length === 0 || totalBooks === 0) return
    setIsSubmitting(true)
    setStatusMessage(null)
    try {
      await api.bulkUpdateTags({
        ids: selectedBookIds,
        add: stagedAddTags,
      })
      setStatusMessage({
        text: `Added ${stagedAddTags.length} tag${stagedAddTags.length !== 1 ? 's' : ''} to ${totalBooks} book${totalBooks !== 1 ? 's' : ''}.`,
        type: 'success',
      })
      setStagedAddTags([])
      refreshAll()
    } catch {
      setStatusMessage({ text: 'Failed to add tags.', type: 'error' })
    } finally {
      setIsSubmitting(false)
    }
  }

  // --- Remove Tab Handlers ---
  const handleToggleRemove = (tagName: string) => {
    setStatusMessage(null)
    setTagsToRemove(prev =>
      prev.includes(tagName) ? prev.filter(t => t !== tagName) : [...prev, tagName]
    )
  }

  const handleSelectAllRemove = () => {
    setStatusMessage(null)
    setTagsToRemove(currentBookTags.map(t => t.name))
  }

  const handleClearSelectedRemove = () => {
    setStatusMessage(null)
    setTagsToRemove([])
  }

  const handleApplyRemove = async () => {
    if (tagsToRemove.length === 0 || totalBooks === 0) return
    setIsSubmitting(true)
    setStatusMessage(null)
    try {
      await api.bulkUpdateTags({
        ids: selectedBookIds,
        remove: tagsToRemove,
      })
      setStatusMessage({
        text: `Removed ${tagsToRemove.length} tag${tagsToRemove.length !== 1 ? 's' : ''} from ${totalBooks} book${totalBooks !== 1 ? 's' : ''}.`,
        type: 'success',
      })
      setTagsToRemove([])
      refreshAll()
    } catch {
      setStatusMessage({ text: 'Failed to remove tags.', type: 'error' })
    } finally {
      setIsSubmitting(false)
    }
  }

  // --- Replace Tab Handlers ---
  const handleApplyReplace = async () => {
    const oldTag = oldTagToReplace.trim()
    const newTag = newTagReplacement.trim()
    if (!oldTag || !newTag || oldTag === newTag || totalBooks === 0) return
    setIsSubmitting(true)
    setStatusMessage(null)
    try {
      await api.bulkUpdateTags({
        ids: selectedBookIds,
        replace: { old_tag: oldTag, new_tag: newTag },
      })
      setStatusMessage({
        text: `Replaced "${oldTag}" with "${newTag}" on selected books.`,
        type: 'success',
      })
      setOldTagToReplace('')
      setNewTagReplacement('')
      refreshAll()
    } catch {
      setStatusMessage({ text: 'Failed to replace tag.', type: 'error' })
    } finally {
      setIsSubmitting(false)
    }
  }

  // Quick suggestions for adding (tags in library not yet staged)
  const addSuggestions = allTags
    .map(t => t.name)
    .filter(name => !stagedAddTags.includes(name))

  const matchingSuggestions = newTagInput.trim()
    ? addSuggestions.filter(name => name.toLowerCase().includes(newTagInput.trim().toLowerCase()))
    : []

  const oldTagDetails = currentBookTags.find(t => t.name === oldTagToReplace)
  const replaceOldTagSelectId = useId()
  const replaceNewTagInputId = useId()

  return (
    <Dialog open={open} onClose={onClose} title="Manage Tags on Multiple Books">
      <div className="p-5 flex flex-col gap-4">
        {/* Header summary badge */}
        <div className="flex items-center justify-between pb-3 border-b border-line">
          <div className="flex items-center gap-2">
            <TagIcon size={16} className="text-accent" />
            <span className="text-sm font-medium text-ink">
              Selected Books: <span className="text-accent font-semibold">{totalBooks}</span>
            </span>
          </div>
          {loadingSummary && (
            <div className="flex items-center gap-1.5 text-xs text-ink-muted">
              <Loader2 size={13} className="animate-spin" />
              <span>Loading tags...</span>
            </div>
          )}
        </div>

        {/* Status message */}
        {statusMessage && (
          <div
            className={`px-3 py-2 rounded text-xs font-medium flex items-center justify-between ${
              statusMessage.type === 'success'
                ? 'bg-emerald-500/10 text-emerald-400 border border-emerald-500/30'
                : 'bg-red-500/10 text-red-400 border border-red-500/30'
            }`}
          >
            <span>{statusMessage.text}</span>
            <button
              type="button"
              onClick={() => setStatusMessage(null)}
              className="opacity-70 hover:opacity-100"
            >
              <X size={12} />
            </button>
          </div>
        )}

        {/* Current tags breakdown */}
        <div>
          <div className="flex items-center justify-between mb-1.5">
            <span className="text-xs font-semibold text-ink-muted uppercase tracking-wider">
              Tags on Selected Books
            </span>
            <span className="text-xs text-ink-muted">
              {currentBookTags.length} tag{currentBookTags.length !== 1 ? 's' : ''} found
            </span>
          </div>

          {currentBookTags.length === 0 ? (
            <p className="text-xs text-ink-muted italic py-1">
              {loadingSummary ? 'Checking tags...' : 'None of the selected books currently have any tags.'}
            </p>
          ) : (
            <div className="flex flex-wrap gap-1.5 max-h-28 overflow-y-auto p-2 rounded-lg bg-surface-raised border border-line">
              {currentBookTags.map(t => {
                const isAll = t.count === totalBooks
                return (
                  <span
                    key={t.id}
                    className={`inline-flex items-center gap-1.5 px-2 py-0.5 rounded-full text-xs font-medium border ${
                      isAll
                        ? 'bg-accent/15 text-accent border-accent/40'
                        : 'bg-surface text-ink-muted border-line'
                    }`}
                    title={isAll ? `On all ${totalBooks} books` : `On ${t.count} of ${totalBooks} books`}
                  >
                    <span>{t.name}</span>
                    <span className="text-[10px] opacity-75 font-mono">
                      {t.count}/{totalBooks}
                    </span>
                  </span>
                )
              })}
            </div>
          )}
        </div>

        {/* Tab selection */}
        <div className="grid grid-cols-3 gap-1 p-1 bg-surface-raised rounded-lg border border-line">
          <button
            type="button"
            onClick={() => { setActiveTab('add'); setStatusMessage(null) }}
            className={`flex items-center justify-center gap-1.5 py-1.5 rounded-md text-xs font-medium transition-colors ${
              activeTab === 'add'
                ? 'bg-surface-card text-ink shadow-sm border border-line-strong'
                : 'text-ink-muted hover:text-ink'
            }`}
          >
            <Plus size={13} />
            <span>Add Tags</span>
          </button>
          <button
            type="button"
            onClick={() => { setActiveTab('remove'); setStatusMessage(null) }}
            className={`flex items-center justify-center gap-1.5 py-1.5 rounded-md text-xs font-medium transition-colors ${
              activeTab === 'remove'
                ? 'bg-surface-card text-ink shadow-sm border border-line-strong'
                : 'text-ink-muted hover:text-ink'
            }`}
          >
            <Trash2 size={13} />
            <span>Remove Tags</span>
          </button>
          <button
            type="button"
            onClick={() => { setActiveTab('replace'); setStatusMessage(null) }}
            className={`flex items-center justify-center gap-1.5 py-1.5 rounded-md text-xs font-medium transition-colors ${
              activeTab === 'replace'
                ? 'bg-surface-card text-ink shadow-sm border border-line-strong'
                : 'text-ink-muted hover:text-ink'
            }`}
          >
            <ArrowRightLeft size={13} />
            <span>Replace Tag</span>
          </button>
        </div>

        {/* Tab Content */}
        {activeTab === 'add' && (
          <div className="flex flex-col gap-3">
            <p className="text-xs text-ink-muted">
              Add tags to all {totalBooks} selected books without changing their other tags.
            </p>

            {/* Input to type / add tag */}
            <div className="flex gap-2">
              <div className="relative flex-1">
                <input
                  type="text"
                  value={newTagInput}
                  onChange={e => setNewTagInput(e.target.value)}
                  onKeyDown={e => {
                    if (e.key === 'Enter') {
                      e.preventDefault()
                      handleStageAdd(newTagInput)
                    }
                  }}
                  placeholder="Type a tag name..."
                  className="w-full px-3 py-2 rounded-lg bg-surface-raised border border-line text-sm text-ink placeholder:text-ink-muted/50 focus:outline-none focus:border-accent"
                />

                {/* Autocomplete suggestion dropdown */}
                {matchingSuggestions.length > 0 && (
                  <div className="absolute z-20 left-0 right-0 top-full mt-1 max-h-36 overflow-y-auto rounded-lg border border-line bg-surface-raised shadow-xl shadow-black/50">
                    {matchingSuggestions.slice(0, 6).map(name => (
                      <button
                        key={name}
                        type="button"
                        onClick={() => handleStageAdd(name)}
                        className="w-full text-left px-3 py-1.5 text-xs text-ink hover:bg-surface-high transition-colors flex items-center justify-between"
                      >
                        <span>{name}</span>
                        <span className="text-[10px] text-ink-muted">Existing</span>
                      </button>
                    ))}
                  </div>
                )}
              </div>

              <button
                type="button"
                onClick={() => handleStageAdd(newTagInput)}
                disabled={!newTagInput.trim()}
                className="px-3.5 py-2 rounded-lg bg-surface-raised border border-line text-xs font-medium text-ink hover:border-line-strong transition-colors disabled:opacity-40 disabled:cursor-not-allowed flex items-center gap-1"
              >
                <Plus size={13} />
                <span>Add</span>
              </button>
            </div>

            {/* Staged tags to apply */}
            {stagedAddTags.length > 0 && (
              <div>
                <span className="text-[11px] font-semibold text-ink-muted uppercase tracking-wider block mb-1">
                  Tags to be added:
                </span>
                <div className="flex flex-wrap gap-1.5 p-2 rounded-lg bg-surface-raised border border-accent/40">
                  {stagedAddTags.map(tag => (
                    <span
                      key={tag}
                      className="inline-flex items-center gap-1 px-2.5 py-1 rounded-md text-xs font-medium bg-accent text-white"
                    >
                      <span>{tag}</span>
                      <button
                        type="button"
                        onClick={() => handleRemoveStagedAdd(tag)}
                        className="hover:opacity-80 transition-opacity"
                        aria-label={`Remove ${tag}`}
                      >
                        <X size={12} />
                      </button>
                    </span>
                  ))}
                </div>
              </div>
            )}

            {/* Quick add existing tags from library */}
            {addSuggestions.length > 0 && (
              <div>
                <span className="text-[11px] font-semibold text-ink-muted uppercase tracking-wider block mb-1">
                  Or pick from library tags:
                </span>
                <div className="flex flex-wrap gap-1 max-h-24 overflow-y-auto">
                  {addSuggestions.slice(0, 15).map(name => (
                    <button
                      key={name}
                      type="button"
                      onClick={() => handleStageAdd(name)}
                      className="px-2 py-0.5 rounded text-xs bg-surface-raised border border-line text-ink-muted hover:text-ink hover:border-line-strong transition-colors flex items-center gap-1"
                    >
                      <Plus size={10} />
                      <span>{name}</span>
                    </button>
                  ))}
                </div>
              </div>
            )}

            <button
              type="button"
              onClick={handleApplyAdd}
              disabled={stagedAddTags.length === 0 || isSubmitting}
              className="mt-2 w-full py-2.5 rounded-lg bg-accent text-white text-sm font-medium hover:bg-accent/90 transition-colors disabled:opacity-40 disabled:cursor-not-allowed flex items-center justify-center gap-2"
            >
              {isSubmitting && <Loader2 size={14} className="animate-spin" />}
              <span>
                Add {stagedAddTags.length} Tag{stagedAddTags.length !== 1 ? 's' : ''} to {totalBooks} Book{totalBooks !== 1 ? 's' : ''}
              </span>
            </button>
          </div>
        )}

        {activeTab === 'remove' && (
          <div className="flex flex-col gap-3">
            <p className="text-xs text-ink-muted">
              Select which tag(s) to remove from the {totalBooks} selected books.
            </p>

            {currentBookTags.length === 0 ? (
              <p className="text-xs text-ink-muted italic py-4 text-center">
                None of the selected books have any tags to remove.
              </p>
            ) : (
              <>
                <div className="flex items-center justify-between text-xs">
                  <span className="text-ink-muted">
                    {tagsToRemove.length} tag{tagsToRemove.length !== 1 ? 's' : ''} selected for removal
                  </span>
                  <div className="flex gap-2">
                    <button
                      type="button"
                      onClick={handleSelectAllRemove}
                      className="text-xs text-accent hover:underline"
                    >
                      Select All
                    </button>
                    {tagsToRemove.length > 0 && (
                      <button
                        type="button"
                        onClick={handleClearSelectedRemove}
                        className="text-xs text-ink-muted hover:underline"
                      >
                        Clear
                      </button>
                    )}
                  </div>
                </div>

                <div className="flex flex-col gap-1 max-h-48 overflow-y-auto rounded-lg border border-line bg-surface-raised p-1.5">
                  {currentBookTags.map(t => {
                    const isSelected = tagsToRemove.includes(t.name)
                    return (
                      <button
                        key={t.id}
                        type="button"
                        onClick={() => handleToggleRemove(t.name)}
                        className={`flex items-center justify-between px-3 py-2 rounded-md text-xs transition-colors ${
                          isSelected
                            ? 'bg-red-500/15 border border-red-500/40 text-red-400'
                            : 'hover:bg-surface-high text-ink'
                        }`}
                      >
                        <div className="flex items-center gap-2">
                          <span
                            className={`w-4 h-4 rounded border flex items-center justify-center ${
                              isSelected ? 'bg-red-500 border-red-500 text-white' : 'border-line-strong'
                            }`}
                          >
                            {isSelected && <Check size={11} strokeWidth={3} />}
                          </span>
                          <span className="font-medium">{t.name}</span>
                        </div>
                        <span className="text-[11px] text-ink-muted">
                          Present on {t.count} of {totalBooks} books
                        </span>
                      </button>
                    )
                  })}
                </div>

                <button
                  type="button"
                  onClick={handleApplyRemove}
                  disabled={tagsToRemove.length === 0 || isSubmitting}
                  className="mt-2 w-full py-2.5 rounded-lg bg-red-500/10 border border-red-500/40 text-red-400 text-sm font-medium hover:bg-red-500/20 transition-colors disabled:opacity-40 disabled:cursor-not-allowed flex items-center justify-center gap-2"
                >
                  {isSubmitting && <Loader2 size={14} className="animate-spin" />}
                  <span>
                    Remove {tagsToRemove.length} Tag{tagsToRemove.length !== 1 ? 's' : ''} from Selected Books
                  </span>
                </button>
              </>
            )}
          </div>
        )}

        {activeTab === 'replace' && (
          <div className="flex flex-col gap-3">
            <p className="text-xs text-ink-muted">
              Find an existing tag on these books and replace it with a new or different tag.
            </p>

            {currentBookTags.length === 0 ? (
              <p className="text-xs text-ink-muted italic py-4 text-center">
                None of the selected books have tags to replace.
              </p>
            ) : (
              <>
                <div>
                  <label htmlFor={replaceOldTagSelectId} className="text-xs font-semibold text-ink-muted uppercase tracking-wider block mb-1">
                    Tag to Replace:
                  </label>
                  <select
                    id={replaceOldTagSelectId}
                    value={oldTagToReplace}
                    onChange={e => setOldTagToReplace(e.target.value)}
                    className="w-full px-3 py-2 rounded-lg bg-surface-raised border border-line text-sm text-ink focus:outline-none focus:border-accent"
                  >
                    <option value="">-- Choose a tag to replace --</option>
                    {currentBookTags.map(t => (
                      <option key={t.id} value={t.name}>
                        {t.name} (on {t.count} books)
                      </option>
                    ))}
                  </select>
                </div>

                <div>
                  <label htmlFor={replaceNewTagInputId} className="text-xs font-semibold text-ink-muted uppercase tracking-wider block mb-1">
                    Replace With:
                  </label>
                  <input
                    id={replaceNewTagInputId}
                    type="text"
                    value={newTagReplacement}
                    onChange={e => setNewTagReplacement(e.target.value)}
                    placeholder="Type new or existing tag name..."
                    className="w-full px-3 py-2 rounded-lg bg-surface-raised border border-line text-sm text-ink placeholder:text-ink-muted/50 focus:outline-none focus:border-accent"
                  />

                  {/* Suggest existing library tags */}
                  {allTags.length > 0 && (
                    <div className="mt-1.5 flex flex-wrap gap-1 max-h-16 overflow-y-auto">
                      {allTags
                        .filter(t => t.name !== oldTagToReplace)
                        .slice(0, 10)
                        .map(t => (
                          <button
                            key={t.id}
                            type="button"
                            onClick={() => setNewTagReplacement(t.name)}
                            className="px-2 py-0.5 rounded text-[11px] bg-surface-raised border border-line text-ink-muted hover:text-ink transition-colors"
                          >
                            {t.name}
                          </button>
                        ))}
                    </div>
                  )}
                </div>

                {oldTagToReplace && newTagReplacement.trim() && (
                  <div className="p-2.5 rounded-lg bg-accent/10 border border-accent/30 text-xs text-ink flex items-center gap-2">
                    <span className="font-medium text-red-400">{oldTagToReplace}</span>
                    <ArrowRightLeft size={12} className="text-accent shrink-0" />
                    <span className="font-medium text-emerald-400">{newTagReplacement.trim()}</span>
                    <span className="text-ink-muted ml-auto">
                      ({oldTagDetails ? `${oldTagDetails.count} book${oldTagDetails.count !== 1 ? 's' : ''}` : ''})
                    </span>
                  </div>
                )}

                <button
                  type="button"
                  onClick={handleApplyReplace}
                  disabled={
                    !oldTagToReplace ||
                    !newTagReplacement.trim() ||
                    oldTagToReplace === newTagReplacement.trim() ||
                    isSubmitting
                  }
                  className="mt-2 w-full py-2.5 rounded-lg bg-accent text-white text-sm font-medium hover:bg-accent/90 transition-colors disabled:opacity-40 disabled:cursor-not-allowed flex items-center justify-center gap-2"
                >
                  {isSubmitting && <Loader2 size={14} className="animate-spin" />}
                  <span>Replace Tag</span>
                </button>
              </>
            )}
          </div>
        )}
      </div>
    </Dialog>
  )
}
