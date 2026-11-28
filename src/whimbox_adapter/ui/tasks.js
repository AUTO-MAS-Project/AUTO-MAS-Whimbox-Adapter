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

  function saveOverride(field, map, key, value) {
    if (value === undefined) delete map[key]
    else map[key] = value
    return save('Task', field, JSON.stringify(map))
  }

  function booleanChoice(value, onChange) {
    const select = document.createElement('select')
    select.add(new Option(text('inherit'), ''))
    select.add(new Option(text('on'), 'true'))
    select.add(new Option(text('off'), 'false'))
    select.value = typeof value === 'boolean' ? String(value) : ''
    select.onchange = () => onChange(select.value === '' ? undefined : select.value === 'true')
    return select
  }

  function optionControl(item) {
    const key = item.key
    const value = options[key]
    const setValue = next => saveOverride('Options', options, key, next)
    if (item.field_type === 'bool') return booleanChoice(value, setValue)
    if (item.field_type === 'select' || item.field_type === 'multi_select') {
      const select = document.createElement('select')
      select.multiple = item.field_type === 'multi_select'
      const inherit = new Option(text('inherit'), '', false, value === undefined)
      select.add(inherit)
      const selected = new Set(select.multiple ? (value ?? []) : [value])
      for (const choice of new Set([...(item.options ?? []), ...selected])) {
        if (typeof choice === 'string')
          select.add(new Option(choice, choice, false, selected.has(choice)))
      }
      select.onchange = () => {
        const choices = [...select.selectedOptions].filter(option => option !== inherit)
        if (inherit.selected && (options[key] !== undefined || !choices.length)) {
          for (const option of choices) option.selected = false
          void setValue(undefined)
        } else {
          inherit.selected = false
          void setValue(select.multiple ? choices.map(option => option.value) : select.value)
        }
      }
      return select
    }
    const input = document.createElement('input')
    input.type = item.field_type === 'int' ? 'number' : 'text'
    input.value = value ?? ''
    input.placeholder = text('inherit')
    input.onchange = () => {
      if (!input.validity.valid) {
        input.reportValidity()
        return
      }
      void setValue(
        input.value === '' ? undefined : input.type === 'number' ? input.valueAsNumber : input.value
      )
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
          panelData.steps.map(step =>
            row(
              step.display,
              booleanChoice(tasks[step.key], value => saveOverride('Tasks', tasks, step.key, value))
            )
          )
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
