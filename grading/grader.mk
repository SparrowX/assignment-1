# Local self-grading. The additional dependency replays chess positions from FEN.
.PHONY: grade test-grader grader-deps
grader-deps:
	@.venv/bin/python -c 'import chess, socksio; assert chess.__version__ == "1.11.2"' 2>/dev/null || uv pip install --python .venv/bin/python chess==1.11.2 socksio==1.0.0

grade: grader-deps
	@.venv/bin/python grading/grade.py $(GRADE_ARGS)

test-grader: grader-deps
	@.venv/bin/python -m pytest -q grading/grader/test_grading.py
