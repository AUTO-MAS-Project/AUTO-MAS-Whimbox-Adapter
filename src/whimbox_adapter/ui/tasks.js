async function mountTasks() {
  const panel = globalThis.connectPanel()
  const controls = document.getElementById('controls')
  const status = document.getElementById('status')
  const steps = document.getElementById('steps')
  const options = document.getElementById('options')
  const labels = [...document.querySelectorAll('[data-i18n]')]
  const keys = ['stepsEmpty', 'optionsEmpty', 'select', 'multiSelect', 'text', 'search', 'catalogFailed', 'saveFailed']
  const prefix = 'panel.tasks.'
  const translationKeys = [...labels.map(label => label.dataset.i18n), ...keys.map(key => prefix + key)]
  let messages
  let catalog
  let overrides
  let translationRequest
  let failure

  function showStatus() {
    const detail = failure ?? catalog?.error
    const key = failure !== undefined ? 'saveFailed' : 'catalogFailed'
    status.textContent = failure !== undefined || !catalog || catalog.error !== undefined
      ? [messages?.[prefix + key], detail].filter(Boolean).join(' · ')
      : ''
    status.toggleAttribute('data-error', Boolean(status.textContent))
  }

  async function saveOverride(field, key, value) {
    const next = { ...overrides[field], [key]: value }
    if (value === null) delete next[key]
    overrides[field] = next
    try {
      const result = await panel.saveConfig([{ group: 'Task', field, value: JSON.stringify(next) }], 'draft')
      failure = result.ok ? undefined : result.message
    } catch (error) {
      failure = error.message
    }
    showStatus()
  }

  function switchInput(label, checked, onChange) {
    label.classList.add('switch-tile')
    const input = document.createElement('input')
    input.type = 'checkbox'
    input.setAttribute('role', 'switch')
    input.checked = checked
    label.prepend(input)
    input.addEventListener('change', () => onChange(input.checked))
  }

  function optionControl(option) {
    const label = document.createElement('label')
    label.textContent = option.display
    const effective = () => (
      Object.hasOwn(overrides.Options, option.key) ? overrides.Options[option.key] : null
    ) ?? option.default
    const save = value => saveOverride('Options', option.key, value)
    const type = option.field_type
    if (type === 'bool') {
      switchInput(label, effective() === true, save)
      return label
    }

    if (type === 'select' || type === 'multi_select') {
      const select = document.createElement('select')
      select.multiple = type === 'multi_select'
      const selected = select.multiple ? effective() ?? [] : [effective()]
      const currentValues = select.multiple ? selected : [effective(), option.default]
      const values = [...new Set([...option.options, ...currentValues.filter(value => value != null)])]
      if (select.multiple) {
        select.setAttribute('aria-description', messages[prefix + 'multiSelect'])
      } else {
        select.append(new Option(messages[prefix + 'select'], ''))
      }
      for (const value of values) select.append(new Option(value, value))
      if (select.multiple) {
        for (const item of select.options) item.selected = selected.includes(item.value)
      } else {
        select.selectedIndex = values.indexOf(effective()) + 1
      }
      let filter
      if (!select.multiple && option.options.length > 8) {
        filter = document.createElement('input')
        filter.type = 'search'
        filter.placeholder = messages[prefix + 'search']
        filter.setAttribute('aria-label', `${option.display} · ${filter.placeholder}`)
        const applyFilter = () => {
          const query = filter.value.trim().toLowerCase()
          for (const item of [...select.options].slice(1)) {
            item.hidden = !item.selected && !item.textContent.toLowerCase().includes(query)
          }
        }
        filter.addEventListener('input', applyFilter)
        select.addEventListener('change', () => queueMicrotask(applyFilter))
      }
      select.addEventListener('change', () => {
        if (select.multiple) {
          const next = new Set([...select.selectedOptions].map(item => item.value))
          const previous = (effective() ?? []).filter(value => next.delete(value))
          void save([...previous, ...next])
        } else if (select.selectedIndex === 0) {
          void save(null)
          select.selectedIndex = values.indexOf(effective()) + 1
        } else {
          void save(select.value)
        }
      })
      label.append(select)
      if (filter) {
        const row = document.createElement('div')
        row.append(filter, label)
        return row
      }
      return label
    }

    const input = document.createElement('input')
    input.type = type === 'int' ? 'number' : 'text'
    const initialValue = effective()
    const hadOverride = Object.hasOwn(overrides.Options, option.key)
    input.value = initialValue ?? ''
    if (type === 'text') input.placeholder = messages[prefix + 'text']
    input.addEventListener('input', () => {
      if (!input.validity.valid) return
      const value = input.value === '' ? null : type === 'int' ? input.valueAsNumber : input.value
      void save(value === initialValue && !hadOverride ? null : value)
    })
    input.addEventListener('change', () => {
      if (input.reportValidity() && input.value === '') input.value = effective() ?? ''
    })
    label.append(input)
    return label
  }

  function render() {
    showStatus()
    controls.hidden = !messages || !catalog || catalog.error !== undefined
    if (controls.hidden) return

    document.getElementById('version').textContent = catalog.upstream_version
      ? `v${catalog.upstream_version}`
      : ''
    steps.replaceChildren()
    for (const { key, display } of catalog.steps) {
      const label = document.createElement('label')
      label.textContent = display
      const checked = Object.hasOwn(overrides.Tasks, key) ? overrides.Tasks[key] ?? true : true
      switchInput(label, checked, value => saveOverride('Tasks', key, value))
      steps.append(label)
    }
    if (!catalog.steps.length) steps.textContent = messages[prefix + 'stepsEmpty']

    // 上游目录没有分组，此处只按键前缀排列控件。
    const rows = new Map()
    options.replaceChildren()
    for (const option of catalog.options) {
      const [group, verbTarget = group] = option.key.split('_')
      const key = group === 'get' ? verbTarget : group
      if (!rows.has(key)) {
        const row = document.createElement('div')
        row.className = 'option-row'
        rows.set(key, row)
        options.append(row)
      }
      rows.get(key).append(optionControl(option))
    }
    if (!catalog.options.length) options.textContent = messages[prefix + 'optionsEmpty']
  }

  function applyConfig({ config: { Task } }) {
    catalog = JSON.parse(Task.Catalog ?? 'null')
    overrides = { Tasks: JSON.parse(Task.Tasks), Options: JSON.parse(Task.Options) }
    failure = undefined
    render()
  }

  async function applyState({ locked, busy, fieldErrors = {}, locale, i18nRevision }) {
    controls.disabled = locked || busy
    for (const field of ['Tasks', 'Options']) {
      document.getElementById(`${field}-error`).textContent = fieldErrors[`Task.${field}`] ?? ''
    }
    if (translationRequest?.locale === locale && translationRequest.i18nRevision === i18nRevision) return
    const request = { locale, i18nRevision }
    translationRequest = request
    const result = await panel.translate(translationKeys)
    if (translationRequest !== request) return
    messages = result.messages
    document.documentElement.lang = result.locale
    for (const label of labels) label.textContent = messages[label.dataset.i18n]
    render()
  }

  const context = await panel.getContext()
  applyConfig(context)
  panel.onConfigReloaded(applyConfig)
  panel.onStateChange(state => void applyState(state).catch(showPanelError))
  await applyState(context)
}

function showPanelError(error) {
  document.getElementById('controls').disabled = true
  const status = document.getElementById('status')
  status.setAttribute('data-error', '')
  status.textContent = error.message
}

mountTasks().catch(showPanelError)
