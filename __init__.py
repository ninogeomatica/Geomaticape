def classFactory(iface):
    from .plugin import GeomaticapePlugin
    return GeomaticapePlugin(iface)