# ParlaSoul Evaluation Gold Dataset Construction Guide

版本：v1.0  
适用阶段：phase18  
配套代码：`src/evaluation/`

## 1. 文档目的

本文档是给负责构建 ParlaSoul 评测数据集的 AI 或工程师的执行规范。执行者只需要阅读本文档和项目代码，就应该能够产出可以被 phase18 evaluation harness 直接加载、校验和运行的 gold dataset。

本数据集必须解决四类问题：

1. 为长期记忆提供可追溯的 evidence、memory ID 和 abstention gold；
2. 为角色对话提供人工 rubric、角色事实和多轮上下文；
3. 为 Judge 提供与生产评价维度一致的人工校准样本；
4. 为 pairwise、OOC、记忆忠实性和记忆效用差异提供可复现 case。

Realtime voice 不使用语义 QA 数据集。语音评测使用单独的功能性 scenario manifest 和 telemetry 断言，见第 11 节。

本文档不允许引入已经从 ParlaSoul 评价契约中删除的独立风格维度。

## 2. 数据集交付物

执行者必须交付一个版本化目录，不得只交一个没有 manifest 的 JSONL 文件：

~~~text
data/evaluation/gold/parlasoul-v1/
├── manifest.json
├── roleplay.jsonl
├── memory.jsonl
├── pairwise.jsonl
├── annotations.jsonl
├── consolidation.jsonl
├── realtime-scenarios.yaml
├── schemas/
│   ├── manifest.schema.json
│   ├── case.schema.json
│   ├── annotation.schema.json
│   └── realtime-scenario.schema.json
└── README.md
~~~

数据集提交策略由 §3 的隐私边界决定：

- **完全 synthetic 的版本**（manifest `data_classification=synthetic`，且不含 §3.2 任何禁止项）：可以直接提交到 Git 公共仓库；smoke、dev、calibration、test 各档均可入仓。
- **含已批准脱敏生产样本或受控 sidecar 的版本**（`synthetic_adjusted` 及以上）：默认不提交 Git，应放在本地或受控对象存储；仓库里只放 manifest、schema、validator、生成脚本与脱敏后的 smoke 子集，并在 manifest 中注明 `source_policy` 与 `artifact_dir`。

任意提交版本的 `manifest.json` 必须真实反映该版本，`scripts/validate_eval_dataset.py` 必须通过；任何 schema/数据/manifest 变化都要重新生成 manifest 并更新 `dataset_fingerprint`。

## 3. 数据来源和隐私边界

### 3.1 允许的数据来源

优先级如下：

1. 人工编写的 synthetic character、dialogue 和 memory case；
2. 经明确授权、去标识化并通过批准 sanitizer 的内部样本；
3. 公开数据集改写后的 case，但必须保留来源、版本和 license 信息。

### 3.2 禁止的数据来源

不得直接把以下内容放入数据集或发给外部 Judge：

- 生产用户 ID、chat ID、turn ID；
- 未脱敏的生产对话正文；
- API key、token、内部 URL、数据库连接信息；
- 可以重新识别用户的姓名、邮箱、电话号码、地址或账号；
- 从生产 candidate 直接复制的完整原文。

如果 case 来自生产事实，必须先生成 approved sidecar，并登记 data_classification、sanitizer_version、sanitizer_hash、approval_scope、approval_expiry、source_fingerprint。

## 4. 数据集总体分层

### 4.1 Role-play 子集

用于评价：

- Persona Fidelity；
- Multi-turn Consistency；
- Coherence & Progression；
- Empathy & Appropriateness；
- Engagement & Diversity；
- Multi-turn OOC；
- Repetition 和 narrative stall 的上下文样本；
- Pairwise Role-play Win Rate。

### 4.2 Memory 子集

用于评价：

- Memory Recall@K；
- Memory Precision@K；
- Injection Hit Rate 的 gold 对照；
- Memory Faithfulness；
- Abstention Accuracy；
- Knowledge Update；
- Temporal Reasoning；
- Memory Utility Δ 的 paired case。

### 4.3 Judge calibration 子集

从 Role-play 和 Memory 子集中抽取固定样本，要求同一 case 同时拥有：

- 2–3 名独立人工评分；
- 经过 adjudication 的最终 gold；
- Judge 使用的同版本 rubric；
- 评分者之间的分歧记录。

该子集用于计算 Judge–Human Agreement 和 Judge Version Drift，不得用 Judge 自己生成的分数充当人工 gold。

### 4.4 Consolidation 子集

每条记录描述一个 consolidation batch：

- 输入 episodic memories；
- 输入已有 semantic memories；
- 期望的新增、更新、删除或保留操作；
- consolidation 前后的 gold 计数；
- 每个输出事实的 supporting source IDs。

Compression Ratio 的最终值仍然必须来自源码运行时的 before/after 统计；数据集中的计数只用于校验运行是否正确。

## 5. 通用 case 契约

每一条文本评测 case 必须是 UTF-8 JSONL，字段如下：

| 字段 | 类型 | 必填 | 规则 |
|---|---|---:|---|
| case_id | string | 是 | 全局唯一，稳定且不可复用 |
| dataset_id | string | 是 | 固定为 parlasoul_gold |
| dataset_version | string | 是 | 例如 v1.0.0 |
| split | enum | 是 | smoke / dev / calibration / test |
| task_type | enum | 是 | 见第 6 节 |
| language | enum | 是 | `zh` / `en` / `mixed`；同一 case 不得混用未声明语言 |
| character_id | UUID/null | 条件 | 有真实受控角色时填写；必须与 character_ref 至少一个，不能产生歧义 |
| character_ref | string | 是 | 受控稳定 identifier，不使用可变 name 猜测 |
| character_snapshot | object | 是 | 角色输入快照，见第 6.1 节 |
| history_turns | array | 是 | 按时间顺序排列的 user/assistant turns |
| user_message | string | 是 | 当前待评估输入 |
| user_message_images | array | 否 | 仅允许已批准的本地 fixture/object key，不允许 URL |
| input_transform_policy | enum | 是 | raw / approved_transformed；后者必须带登记的 version/hash |
| input_transform_version/hash | string/null | 条件 | raw 时必须为空；approved_transformed 时必须与 sidecar 登记一致 |
| memory_snapshot | object/null | 否 | 记忆 case 必填，见第 6.2 节 |
| expected_keywords | array | 否 | 只作为明确 rubric 的辅助输入，不是命中即得分规则 |
| expected_behavior | string | 条件 | 角色行为约束，不能写成隐式分数规则 |
| expected_answer | string/null | 条件 | 有明确答案的 memory case 必填 |
| expected_claims | array | 条件 | Faithfulness case 必填 |
| gold_memory_ids | array | 条件 | Recall/Precision case 必填 |
| supporting_memory_ids | array | 条件 | Faithfulness case 必填 |
| should_abstain | boolean/null | 条件 | Abstention case 必填 |
| ooc_label | boolean/null | 条件 | OOC case 必填 |
| dimension_weights | object | 否 | 只允许五个角色维度/OOC 名称；仅用于分维展示，不生成跨域总分 |
| source_candidate_id | UUID/null | 条件 | 仅批准的脱敏 sidecar 可引用，不能授权直接读取生产正文 |
| tags | array | 是 | 用于分桶，不得包含用户身份信息 |

### 5.1 通用 JSON 示例

~~~json
{
  "case_id": "memory-temporal-001",
  "dataset_id": "parlasoul_gold",
  "dataset_version": "v1.0.0",
  "split": "test",
  "task_type": "memory_temporal",
  "language": "en",
  "character_ref": "character.synthetic.alex",
  "character_snapshot": {
    "name": "Alex",
    "description": "A thoughtful language-learning companion.",
    "system_prompt": "Approved synthetic character prompt",
    "facts": [
      {
        "fact_id": "fact-1",
        "text": "The user prefers jazz.",
        "valid_from": "2026-01-01",
        "valid_to": null
      }
    ],
    "relationship_state": ["new_friend"]
  },
  "history_turns": [
    {"role": "user", "content": "I have been listening to jazz lately."},
    {"role": "assistant", "content": "That sounds lovely."}
  ],
  "user_message": "What kind of music do I like?",
  "memory_snapshot": {
    "episodic": [
      {
        "memory_id": "m-1",
        "source_turn_id": "synthetic-turn-1",
        "text": "The user prefers jazz.",
        "memory_type": "episodic",
        "created_at": "2026-01-01T00:00:00Z",
        "valid_from": "2026-01-01T00:00:00Z",
        "valid_to": null,
        "group_id": null
      }
    ],
    "semantic": []
  },
  "expected_answer": "The user likes jazz.",
  "gold_memory_ids": ["m-1"],
  "supporting_memory_ids": ["m-1"],
  "expected_claims": [
    {
      "claim_id": "claim-1",
      "text": "The user prefers jazz.",
      "supporting_memory_ids": ["m-1"],
      "is_required": true
    }
  ],
  "should_abstain": false,
  "ooc_label": false,
  "tags": ["single_hop", "positive_evidence"]
}
~~~

### 5.2 manifest.json 与 fingerprint

`manifest.json` 必须包含以下字段，并且 `fingerprint` 不参与自身 hash：

~~~json
{
  "dataset_id": "parlasoul_gold",
  "dataset_version": "v1.0.0",
  "schema_version": "phase18.case.v1",
  "language": "en",
  "files": {
    "roleplay": {"path": "roleplay.jsonl", "records": 300, "sha256": "<64 lowercase hex>"},
    "memory": {"path": "memory.jsonl", "records": 500, "sha256": "<64 lowercase hex>"},
    "pairwise": {"path": "pairwise.jsonl", "records": 200, "sha256": "<64 lowercase hex>"},
    "annotations": {"path": "annotations.jsonl", "records": 100, "sha256": "<64 lowercase hex>"},
    "consolidation": {"path": "consolidation.jsonl", "records": 100, "sha256": "<64 lowercase hex>"},
    "realtime": {"path": "realtime-scenarios.yaml", "records": 30, "sha256": "<64 lowercase hex>"}
  },
  "splits": {
    "text_cases": {"smoke": 4, "dev": 600, "calibration": 100, "test": 96},
    "pairwise_cases": {"dev": 100, "calibration": 50, "test": 50},
    "realtime_scenarios": {"test": 30}
  },
  "task_types": ["roleplay_persona", "memory_recall", "pairwise_roleplay"],
  "rubric_version": "parlasoul-roleplay-5d-ooc-v1",
  "sanitizer_version": "synthetic-only-v1",
  "data_classification": "synthetic",
  "source_policy": {"production_text": false, "approved_sidecar": false},
  "created_at": "2026-08-14T00:00:00Z",
  "fingerprint_algorithm": "sha256(canonical-json(file-path,bytes-hash,record-count,versions))",
  "fingerprint": "<64 lowercase hex>"
}
~~~

生成规则：每个文件先按 UTF-8 原始字节计算 SHA-256；再把 `files` 按路径排序、manifest 其他键按字典序排序，以无空格 UTF-8 canonical JSON 序列化后计算 dataset fingerprint。不得把绝对路径、机器名、时间戳之外的随机值写入 fingerprint 输入；修改任意 case、schema、标注或版本都必须改变 fingerprint。`README.md` 只说明来源和构建命令，不纳入数据 fingerprint。

### 5.3 pairwise.jsonl

每行是一个 `PairwiseCase`，必须引用同一 `base_context_fingerprint` 的两个 variant：

~~~json
{
  "case_id": "pair-roleplay-001",
  "dataset_id": "parlasoul_gold",
  "dataset_version": "v1.0.0",
  "split": "test",
  "task_type": "pairwise_roleplay",
  "base_case_id": "roleplay-base-001",
  "base_context_fingerprint": "<64 lowercase hex>",
  "variant_a": {
    "variant_ref": "synthetic.variant.a.001",
    "content": "Anonymized candidate A.",
    "generation_provider": "fixture",
    "generation_model": "fixture-model-a",
    "generation_model_type": "fixture"
  },
  "variant_b": {
    "variant_ref": "synthetic.variant.b.001",
    "content": "Anonymized candidate B.",
    "generation_provider": "fixture",
    "generation_model": "fixture-model-b",
    "generation_model_type": "fixture"
  },
  "order_swap": true,
  "human_winner": "a"
}
~~~

`human_winner` 只能是 `a`、`b`、`tie` 或 `invalid`，仅供 calibration gold 使用；Judge 的两次结果必须在运行时重新计算。生产 source mode 可以省略 `content`，但 `variant_ref` 必须能解析到已批准 sidecar，且 sidecar fingerprint、generation route/model 和 base fingerprint 必须全部匹配。

### 5.4 annotations.jsonl

每行对应一名标注员对一个 case 的独立标注；标注员 ID 必须是不可逆 opaque ID，不能是姓名、邮箱或账号：

~~~json
{
  "annotation_id": "ann-001",
  "case_id": "roleplay-base-001",
  "annotator_ref": "annotator-opaque-01",
  "rubric_version": "parlasoul-roleplay-5d-ooc-v1",
  "target": {
    "response_ref": "synthetic.response.001",
    "content": "Anonymized candidate response for calibration.",
    "source_fingerprint": "<64 lowercase hex>"
  },
  "scores": {
    "persona_fidelity": 4,
    "multi_turn_consistency": 5,
    "coherence_progression": 4,
    "empathy_appropriateness": 4,
    "engagement_diversity": 3
  },
  "ooc_label": false,
  "memory_labels": {
    "relevant_memory_ids": ["m-1"],
    "distractor_memory_ids": ["m-2"],
    "expired_memory_ids": [],
    "supporting_memory_ids": ["m-1"],
    "should_abstain": false
  },
  "pairwise_winner": null,
  "adjudication": null
}
~~~

同一 calibration case 至少有两条不同 `annotator_ref`，并且 `target.response_ref` 必须相同；`target.content` 仅允许 synthetic/已批准脱敏文本，生产样本可省略正文但必须能解析批准 sidecar。分数差大于 1、OOC 标签不一致、memory evidence 不一致或 pairwise winner 不一致必须增加第三人 `adjudication` 记录。最终 gold 由 adjudication 写入独立字段，不能覆盖原始评分。`scores` 的 key 只能是五个角色维度，禁止添加独立的风格维度字段。

### 5.5 consolidation.jsonl

每行描述一个可重放的 consolidation batch。计数是预期值，不替代源码 hook 的实际统计：

~~~json
{
  "batch_id": "consolidation-001",
  "dataset_id": "parlasoul_gold",
  "dataset_version": "v1.0.0",
  "episodic_memories": [
    {"memory_id": "e-1", "text": "The user likes jazz.", "source_fingerprint": "<64 lowercase hex>"},
    {"memory_id": "e-2", "text": "The user likes jazz music.", "source_fingerprint": "<64 lowercase hex>"}
  ],
  "semantic_memories": [],
  "expected_actions": [
    {"action": "deduplicate", "source_memory_ids": ["e-1", "e-2"], "output_memory_id": "s-1"}
  ],
  "expected_before": {"episodic_count": 2, "semantic_count": 0, "physical_total_count": 2, "logical_fact_count": 2},
  "expected_after": {"episodic_count": 2, "semantic_count": 1, "physical_total_count": 3, "logical_fact_count": 1},
  "supporting_source_ids": ["e-1", "e-2"]
}
~~~

`expected_actions.action` 只能是 `create`、`update`、`deduplicate`、`merge`、`delete`、`retain` 或 `no_op`。由于 ParlaSoul 保留源 episodic memory，物理行数可能因新建 semantic memory 而增加；实际 Compression Ratio 必须读取运行时可比的 `logical_before_count/logical_after_count` 和 `action_counts`，公式为 `1 - logical_after_count / logical_before_count`。若 fixture 与 hook 统计不一致，case 失败而不是修改 gold 迎合结果。

## 6. Role-play 和 Memory 的专用字段

### 6.1 Character snapshot

character_snapshot 必须是 case 运行时实际可见的角色输入，不得只写一个角色名字。至少包含：

- name；
- description；
- approved system_prompt 或其受控引用；
- 稳定 persona facts；
- 关系状态；
- 角色禁止行为；
- 当前历史中已经确认的事实。

人工标注员只能依据 case 中提供的 snapshot 和 history 评分，不能访问隐藏答案或模型身份。

### 6.2 Memory snapshot

每条 memory 必须有稳定 memory_id。禁止使用数组下标作为 ID。建议字段：

- memory_id；
- source_turn_id；
- text；
- memory_type：episodic / semantic；
- created_at；
- valid_from / valid_to；
- group_id；
- is_conflicting；
- source_fingerprint。

同一事实的更新必须保留不同 memory ID，不能覆盖旧事实。这样才能评估 knowledge update、冲突处理和 temporal reasoning。

`memory_snapshot.episodic` 只能放 `memory_type=episodic` 的条目，`semantic` 只能放 `memory_type=semantic` 的条目；同一 case 内所有 `memory_id` 必须全局唯一。

### 6.3 Task type 枚举

~~~text
roleplay_persona
roleplay_consistency
roleplay_coherence
roleplay_empathy
roleplay_engagement
memory_recall
memory_precision
memory_update
memory_temporal
memory_faithfulness
memory_abstention
memory_ooc
memory_utility_pair
pairwise_roleplay
pairwise_memory
~~~

## 7. 人工标注规范

### 7.1 五个角色维度

每维 1–5 分，禁止使用整体印象代替逐维评分。

#### Persona Fidelity

- 1：明显违背角色核心事实或角色身份；
- 2：多处违背或出现严重出戏；
- 3：基本符合但有明显不稳定点；
- 4：符合角色事实和关系状态，仅有轻微偏差；
- 5：完全符合角色契约，且回应自然。

#### Multi-turn Consistency

检查角色立场、已确认事实、关系阶段和时间顺序是否自洽：

- 1：直接否定或改写多个已确认事实，关系/时间完全断裂；
- 2：存在至少一个严重矛盾，后续行为无法由历史解释；
- 3：主线一致，但有一处明显事实、关系或时间不稳定；
- 4：与历史基本一致，只有不影响理解的轻微遗漏；
- 5：事实、关系、时间和角色立场均连续且能自然承接。

#### Coherence & Progression

检查是否真正回应当前语境、是否推动对话、是否重复原地循环：

- 1：没有回应问题或完全无法理解；
- 2：只复述输入、跳题或明显循环；
- 3：基本回应但推进很弱或有局部断裂；
- 4：回应重点清楚并带来合理的下一步；
- 5：紧扣语境，信息组织自然，并有效推进互动。

#### Empathy & Appropriateness

检查情绪识别、回应强度、边界意识和关系语气是否合适：

- 1：忽视或伤害明显情绪，越界或给出危险建议；
- 2：情绪判断明显错误，语气/强度严重不当；
- 3：基本礼貌但回应机械或支持不足；
- 4：能识别情绪，强度和边界基本合适；
- 5：对情绪、关系和风险高度敏感，回应既自然又克制。

#### Engagement & Diversity

检查是否机械模板化、是否有无意义重复、是否提供自然的继续交流空间：

- 1：高度模板化、重复或终止互动；
- 2：明显机器腔，只有单一套路或空泛追问；
- 3：可继续但缺少新信息/变化；
- 4：表达有变化，提供自然的继续交流入口；
- 5：内容具体、有新意且不喧宾夺主，能持续吸引对话。

### 7.2 Memory 标注

每个 memory case 必须回答：

1. 哪些 memory 是相关证据；
2. 哪些 memory 是干扰项；
3. 哪些 memory 已过期；
4. 哪些 memory 互相冲突；
5. 回复中的每个事实由哪些 memory 支持；
6. 在没有充分证据时是否必须 abstain。

### 7.3 OOC 标注

OOC case 必须明确说明：

- 违反了哪个 persona fact；
- 违反了哪个关系状态；
- 是否属于时间矛盾；
- 是否属于语气不当但事实没有矛盾；
- 是否属于纯粹的低质量而非 OOC。

不能把“回复不好”直接标为 OOC。

### 7.4 Pairwise 标注

人工只看匿名的 A/B 回复，不看模型、provider、版本和生成顺序。结果只能是：

~~~text
a
b
tie
invalid
~~~

invalid 表示任一回复为空、解析失败、包含不可评估错误或 case 上下文不一致，不进入胜率分母。

## 8. 数据构建流程

### Step 1：建立角色和事实库

先创建 synthetic character catalog。每个角色必须有：

- persona facts；
- 禁止事实；
- 关系状态变迁；
- 典型情绪和边界场景；
- 至少一组长期记忆和冲突记忆。

### Step 2：编写多轮历史

每个 history 必须包含真实评测所需的上下文，不允许把答案直接写进当前 user message。至少包含：

- single-hop fact；
- multi-hop fact；
- temporal update；
- preference change；
- irrelevant distractor；
- missing-memory probe；
- contradiction probe。

### Step 3：生成 candidate 草稿

可以使用模型生成初稿，但模型生成内容只能作为 draft：

1. 记录生成模型和 prompt 版本；
2. 人工检查事实、时间和证据链接；
3. 人工修改不自然或有歧义的 case；
4. 最终 gold 不得直接由同一个 Judge 自动确认。

### Step 4：建立 evidence graph

为每个答案事实建立：

~~~text
claim_id → supporting_memory_ids → source_turn_id
~~~

所有 gold_memory_ids 都必须存在于 memory_snapshot，所有 supporting_memory_ids 都必须存在于对应 case 的 memory 集合。

### Step 5：制作负例和困难例

必须包含：

- 记忆存在但不相关；
- 记忆过期；
- 新旧偏好冲突；
- 只检索到部分证据；
- 没有任何可支持证据；
- 回复事实正确但角色表达不一致；
- 回复自然但违反 persona；
- 看似合理但实际 OOC。

### Step 6：人工双标和 adjudication

- 每条 calibration case 至少 2 名独立标注员；
- 分歧超过 1 分或 pairwise 结果不一致时进入第三人 adjudication；
- 记录最终 gold、原始评分和 adjudication 理由；
- 标注员不能看到模型身份和版本；
- 每次 rubric 变化必须重新标注受影响的 case。

## 9. 推荐最小规模

这是 phase18 第一版建议值，不是永久上限：

| 子集 | 建议数量 | 最低覆盖要求 |
|---|---:|---|
| Role-play | 300 | 多角色、多轮、事实冲突、情绪和推进 |
| Memory | 500 | recall、update、temporal、faithfulness、abstention 各有正负例 |
| Pairwise | 200 对 | 两个版本、两个模型档位、不同角色和场景 |
| Judge calibration | 100 | 每个启用维度均有人工重复评分 |
| Consolidation | 100 batch | dedup、update、conflict、no-op |
| Realtime scenario | 30 起 | 正常、打断、网络降级、重连、超时、异常关闭 |

应优先保证分层覆盖和标注质量，不要用大量重复 case 替代困难例。

## 10. 数据质量门禁

构建完成后必须运行 validator，至少检查：

1. 所有文件是 UTF-8；
2. case_id 全局唯一；
3. dataset_version、manifest fingerprint 一致；
4. 所有 memory/source ID 可解析；
5. gold_memory_ids 不引用不存在的 memory；
6. abstention case 不得同时声明充分 evidence；
7. update case 至少包含两个时间或版本状态；
8. pairwise 两侧 context fingerprint 完全一致；
9. train/dev/test 不共享同一底层 conversation 或 character history；
10. calibration case 不泄露 model/provider/version；
11. 不包含用户身份、secret、生产原文或未经批准的图片 URL；
12. 每个 task type 都有正例、负例和 no-data 边界；
13. 标注分歧、adjudication 和缺失字段都有统计报告。

建议新增：

~~~text
scripts/validate_eval_dataset.py
~~~

validator 失败时必须返回非零退出码，phase18 runner 不得继续执行。

## 11. Realtime 功能 scenario

Realtime 不保存语义答案，只保存 scenario 和断言：

~~~yaml
scenario_id: realtime-barge-in-001
network_profile: wifi_normal
actions:
  - type: connect
  - type: send_user_audio_fixture
  - type: barge_in
    at_ms: 900
assertions:
  - event: assistant_audio_started
    within_ms: 1800
  - event: assistant_audio_stopped
    within_ms: 300
  - event: next_turn_started
    within_ms: 1200
metrics:
  - ttfa_ms
  - interruption_stop_latency_ms
  - reconnect_count
  - webrtc_rtt_ms
  - jitter_ms
  - packet_loss_ratio
~~~

scenario runner 必须：

- 使用真实 realtime pipeline；
- 不保存原始音频和完整转写；
- 保存事件时间戳、状态、错误阶段和网络指标；
- 明确 passed / failed / no_data / cancelled；
- 对同一 scenario 支持重复运行和 fingerprint；
- 把 P50/P95 聚合与原始 waterfall 分开报告。

## 12. phase18 对接契约

数据集构建完成后，phase18 必须支持：

1. GoldenCase 加载、Pydantic 校验和 dataset fingerprint；
2. Role-play、Memory、Pairwise、Consolidation 四类 JSONL loader；
3. calibration annotation loader；
4. memory snapshot 注入到无副作用 replay；
5. retrieval trace：retrieved IDs、distance、seed/expanded 标记；
6. used-memory trace：MemoryUsageJudge 输出的 used IDs；
7. retrieval timing 持久化；
8. consolidation before/after stats；
9. memory-on/off 配对执行；
10. 五个角色维度和 OOC 的 Judge + human calibration；
11. Pairwise order-swap、tie、invalid 和有效分母；
12. Realtime scenario runner 和 telemetry 聚合；
13. eval_runs/eval_results 的版本、状态、RLS、保留和清理策略。

## 13. 交付验收

数据集构建任务只有同时满足以下条件才算完成：

- manifest、schema、JSONL/YAML 文件齐全；
- validator 全部通过；
- smoke fixture 可以被 phase18 loader 加载；
- calibration case 有人工原始评分和 adjudicated gold；
- 每个指标都能追溯到 case、evidence 或 runtime trace；
- 不使用默认分数填充缺失标签；
- 缺失 evidence、缺失人工评分和无 telemetry 都报告为 unavailable 或 no_data；
- 数据集版本和 fingerprint 可复现；
- 文本内容通过隐私和授权检查；
- 其他 AI 可以只根据本指南和项目代码完成构建，不需要口头补充规则。
