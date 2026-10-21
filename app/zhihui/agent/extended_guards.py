"""Additional tool guards layered on the unchanged original Guard."""
import re
from .guards import Guard, user_text


class ExtendedGuard(Guard):
    def normalize(self, name, args, messages):
        super().normalize(name, args, messages)
        if name=='edit_title_block':
            aliases={'设计单位':'unit_name','单位名称':'unit_name','工程名称':'project','图名':'drawing_name','图号':'drawing_no','日期':'date','设计阶段':'stage','比例说明':'scale_text','专业':'discipline'}
            args['fields']={aliases.get(k,k):v for k,v in args.get('fields',{}).items()}
        if name in {'edit_title_block', 'edit_annotation'}:
            args['user_facts'] = {'text': user_text(messages)}
        return args

    def check(self, name, args, messages):
        if name == 'add_instrument':
            # The loop number is the digit part of an instrument tag the user wrote (FT-101); the base guard strips tag digits as "not values".
            loops = ' '.join(re.findall(r'(?<![A-Za-z0-9])[A-Z]{2,3}-(\d{3,5})(?![A-Za-z0-9])', user_text(messages)))
            if loops:
                messages = list(messages) + [{'role': 'user', 'content': loops}]
        blocked = super().check(name, args, messages)
        if blocked:
            return blocked
        if name == 'delete_annotation':
            said = user_text(messages)
            ref = str(args.get('annotation_ref', ''))
            if not args.get('confirmed') or ref not in said or not re.search('确认|同意', said):
                return {'ok': False, 'error_code': 'E_TARGET_UNCONFIRMED', 'message': f'请明确确认删除标注 {ref}', 'hint': '确认删除后重试'}
        return None
