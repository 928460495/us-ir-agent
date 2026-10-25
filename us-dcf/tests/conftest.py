# test_mutation*.py 是变异脚本：导入即执行，并从 sys.argv[1] / US_TEST_XLSX 读模型文件，
# 不是 pytest 测试。手动运行：US_TEST_XLSX=模型.xlsx python tests/test_mutation.py
import os
import sys

collect_ignore = ["test_mutation.py", "test_mutation_ic.py"]
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
