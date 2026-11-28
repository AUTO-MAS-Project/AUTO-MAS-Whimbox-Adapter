async function mountPanel() {
  const panel = globalThis.connectPanel()
  const controls = document.getElementById('controls')
  const status = document.getElementById('status')
  const labels = [...document.querySelectorAll('[data-text]')]
  const keys = [
    ...new Set([
      ...labels.map(node => node.dataset.text),
      'missingRoot',
      'unavailable',
      'empty',
      'inherit',
      'version',
    ]),
  ]
  let messages = {}
  let language
  let context
  const text = key => messages['panel.' + key] || ''
  const showError = message => {
    status.textContent = message || ''
  }

  async function save(group, field, value) {
    try {
      const result = await panel.saveConfig([{ group, field, value }], 'draft')
      if (!result.ok) throw new Error(result.message || result.code)
      showError('')
    } catch (error) {
      applyConfig(await panel.getContext())
      showError(error.message)
    }
  }

  const view = createView({ panel, save, text, showError })
  function applyConfig(updated) {
    context = updated
    showError('')
    view.render(context)
  }
  async function applyState(state) {
    controls.disabled = state.locked || state.busy
    if (state.fieldErrors && Object.keys(state.fieldErrors).length) {
      showError(Object.values(state.fieldErrors).join('\n'))
    }
    const next = state.locale + '/' + state.i18nRevision
    if (language === next) return
    language = next
    const translated = await panel.translate(keys.map(key => 'panel.' + key))
    if (language !== next) return
    messages = translated.messages
    document.documentElement.lang = translated.locale
    for (const node of labels) node.textContent = text(node.dataset.text)
    if (context) {
      const latest = await panel.getContext()
      if (language === next) applyConfig(latest)
    }
  }

  const initial = await panel.getContext()
  panel.onStateChange(state => {
    void applyState(state).catch(error => showError(error.message))
  })
  panel.onConfigReloaded(applyConfig)
  applyConfig(initial)
  await applyState(initial)
}

mountPanel().catch(error => {
  document.getElementById('controls').disabled = true
  document.getElementById('status').textContent = error.message
})
