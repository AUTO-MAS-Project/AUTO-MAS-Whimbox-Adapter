function createView({ save, text, showError }) {
  const accounts = document.getElementById('accounts')
  const columns = document.getElementById('columns')
  const version = document.getElementById('version')
  let tasks, options
  accounts.onchange = () => save('OneDragon', 'IfRunAllAccounts', accounts.checked)

  function row(title, control) {
    const label = document.createElement('label')
    const caption = document.createElement('span')
    caption.textContent = title
    label.append(caption, control)
    return label
  }

  function checkbox() {
    const input = document.createElement('input')
    input.type = 'checkbox'
    input.setAttribute('role', 'switch')
    return input
  }

  function setOption(key, value) {
    if (value === undefined) delete options[key]
    else options[key] = value
    return save('Task', 'Options', JSON.stringify(options))
  }

  function optionControl(item) {
    const key = item.key
    const value = options[key] ?? item.default
    if (item.field_type === 'bool') {
      const input = checkbox()
      input.checked = value === true
      input.onchange = () => setOption(key, input.checked)
      return input
    }
    if (item.field_type === 'select' || item.field_type === 'multi_select') {
      const select = document.createElement('select')
      select.multiple = item.field_type === 'multi_select'
      if (!select.multiple) select.add(new Option(text('inherit'), ''))
      const selected = new Set(select.multiple ? (value ?? []) : [value])
      for (const choice of new Set([...(item.options ?? []), ...selected])) {
        if (typeof choice === 'string' && choice)
          select.add(new Option(choice, choice, false, selected.has(choice)))
      }
      select.onchange = () => {
        const next = select.multiple
          ? [...select.selectedOptions].map(o => o.value)
          : select.value || undefined
        void setOption(key, next)
        if (next === undefined) select.value = item.default ?? ''
      }
      return select
    }
    const input = document.createElement('input')
    input.type = item.field_type === 'int' ? 'number' : 'text'
    input.value = value ?? ''
    input.onchange = () => {
      if (!input.validity.valid) {
        input.reportValidity()
        return
      }
      const next =
        input.value === '' ? undefined : input.type === 'number' ? input.valueAsNumber : input.value
      void setOption(key, next)
      if (next === undefined) input.value = item.default ?? ''
    }
    return input
  }

  function fill(id, rows) {
    const container = document.getElementById(id)
    container.replaceChildren(...rows)
    if (!rows.length) container.textContent = text('empty')
  }

  function overrides(raw) {
    const parsed = JSON.parse(raw ?? '{}')
    if (!parsed || Array.isArray(parsed) || typeof parsed !== 'object')
      throw new Error(text('unavailable'))
    // Upstream owns the keys; treat every key as data.
    return Object.assign(Object.create(null), parsed)
  }

  return {
    render({ config, panelData }) {
      accounts.checked = !!config.OneDragon.IfRunAllAccounts
      columns.hidden = true
      version.textContent = ''
      if (!panelData || panelData.error || panelData.errorKey) {
        showError(
          panelData?.error ||
            text(panelData?.errorKey === 'panel.missingRoot' ? 'missingRoot' : 'unavailable')
        )
        return
      }
      try {
        tasks = overrides(config.Task.Tasks)
        options = overrides(config.Task.Options)
        fill(
          'steps',
          panelData.steps.map(step => {
            const input = checkbox()
            input.checked = tasks[step.key] ?? true
            input.onchange = () => {
              tasks[step.key] = input.checked
              void save('Task', 'Tasks', JSON.stringify(tasks))
            }
            return row(step.display, input)
          })
        )
        fill(
          'options',
          panelData.options.map(item => row(item.display, optionControl(item)))
        )
        version.textContent = panelData.upstream_version
          ? text('version') + ' ' + panelData.upstream_version
          : ''
        columns.hidden = false
      } catch (error) {
        showError(error.message)
      }
    },
  }
}
