def single_job_settings(original):
    # ARQ reads vars(settings), not inherited attributes. Copy the registered
    # functions and hooks before overriding concurrency.
    values = {key: value for key, value in vars(original).items() if not key.startswith('__')}
    values['max_jobs'] = 1
    return type('Settings', (), values)
