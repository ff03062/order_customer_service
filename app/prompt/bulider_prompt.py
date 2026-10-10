import os
import yaml


class BuilderPromptYaml:
    @staticmethod
    def get_prompt(file_name: str) -> str:
        base_dir = os.path.dirname(os.path.abspath(__file__))
        file_path = os.path.join(base_dir, file_name)

        with open(file_path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)
        prompt = (
            f"一 角色:{config['role']}\n"
            f"二 任务:\n{chr(10).join(config['task'])}\n"
            f"三 规则:\n{chr(10).join(config['rule'])}\n"
            f"四 输出:\n{chr(10).join(config['output'])}\n"
            f"五 示例:\n{chr(10).join(config['example'])}\n"
        )
        print(f"提示词：{os.path.basename(file_path)}构建成功")
        return prompt.strip()
