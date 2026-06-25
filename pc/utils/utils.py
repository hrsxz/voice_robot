import re


def normalize_text(text: str) -> str:
    # 将输入文本转换为半角、去掉大部分符号、统一大小写，便于后续规则匹配和参数提取
    def _to_half_width(text: str) -> str:
        chars = []
        for ch in text:
            code = ord(ch)
            if code == 0x3000:
                code = 0x0020
            elif 0xFF01 <= code <= 0xFF5E:
                code -= 0xFEE0
            chars.append(chr(code))
        return ''.join(chars)

    if not text:
        return ''
    # 统一空格、全角数字和全角标点，便于后续规则匹配
    normalized = _to_half_width(str(text).strip()).lower()
    # 去掉大部分符号，但保留中文、英文、数字和空白
    normalized = re.sub(r"[^\w\s\u4e00-\u9fff]+", ' ', normalized)
    # 把连续空白压缩成一个空格，避免分词和正则提取受干扰
    normalized = re.sub(r"\s+", ' ', normalized)

    return normalized.strip()
