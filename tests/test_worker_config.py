def test_arq_sees_copied_functions_in_settings_dictionary():
    from scripts.worker_settings import single_job_settings
    class Original:
        functions = ['render']
        max_jobs = 4
        queue_name = 'supoclip_tasks'
    settings = single_job_settings(Original)
    assert vars(settings)['functions'] == ['render']
    assert vars(settings)['max_jobs'] == 1
