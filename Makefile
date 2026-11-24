.PHONY: review review-staged review-install review-rules review-rules-quality review-fixtures-quality review-engine review-respond review-engine-respond

review:
	@python3 .clarity/engine/runner/review.py review --target HEAD

review-staged:
	@python3 .clarity/engine/runner/review.py review --target index

review-rules:
	@python3 .clarity/engine/runner/review.py check-rules

review-rules-quality:
	@python3 .clarity/engine/runner/review.py check-rules-quality

review-fixtures-quality:
	@python3 .clarity/engine/runner/review.py check-fixtures-quality

review-engine:
	@python3 .clarity/engine/runner/review.py maintain

# 回应是原话，必须按字面量传入环境，避免 Make 或 shell 执行其中的金额、命令及换行。
# 命令行变量本会自动导出；禁止再次展开原 RESPONSE，只导出已按字面读取的值。
unexport RESPONSE
review-respond review-engine-respond: export CLARITY_RESPONSE := $(value RESPONSE)

review-respond:
	@python3 .clarity/engine/runner/review.py respond --text-env CLARITY_RESPONSE

review-engine-respond:
	@python3 .clarity/engine/runner/review.py maintain-respond --text-env CLARITY_RESPONSE

review-install:
	@sh .clarity/engine/runner/commit-msg --install
