def get_parser_class(msg, parser_name):
    # Choose the parser we want to use
    if parser_name == 'aie':
        msg.debug('Using AnimeInfoExtractor parser')
        from .animeinfoextractor import AnimeInfoExtractor
        return AnimeInfoExtractor
    elif parser_name == 'anitopy':
        msg.debug('Using Anitopy parser')
        from .anitopy import AnitopyWrapper
        return AnitopyWrapper
    elif parser_name == 'anitomy_ng':
        msg.debug('Using anitomy-ng parser')
        from .anitomy_ng import AnitomyNgWrapper
        return AnitomyNgWrapper
    else:
        msg.debug('Unknown parser "{}", falling back to default'.format(parser_name))
        return get_parser_class(msg, 'aie')
