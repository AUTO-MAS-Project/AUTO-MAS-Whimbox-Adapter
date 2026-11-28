function createView({ panel, save, text, showError }) {
  const name = document.getElementById('name')
  const root = document.getElementById('root')
  const version = document.getElementById('version')
  name.onchange = () => save('Info', 'Name', name.value)
  root.onchange = () => save('Info', 'RootPath', root.value || null)
  document.getElementById('browse').onclick = async () => {
    try {
      const result = await panel.pickPath({
        mode: 'folder',
        defaultPath: root.value,
        title: text('browse'),
      })
      if (result.code === 'unavailable') showError(text('unavailable'))
      if (!result.path) return
      root.value = result.path
      await save('Info', 'RootPath', root.value)
    } catch (error) {
      showError(error.message)
    }
  }
  return {
    render({ config, panelData }) {
      name.value = config.Info.Name ?? ''
      root.value = config.Info.RootPath ?? ''
      version.textContent = panelData?.version ? text('version') + ' ' + panelData.version : ''
    },
  }
}
