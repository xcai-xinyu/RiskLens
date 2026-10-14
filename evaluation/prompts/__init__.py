from pathlib import Path


def raw_messages(request):
    return [('system',(Path(__file__).parent/'raw_p4_v1.txt').read_text()),
            ('human',request.model_dump_json())]
