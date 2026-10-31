# 二次开发:新增子任务、数据集与替换底座

## 1. 本讲目标

前面六个单元里,我们沿着「数据 → 3D 主干 → LMM → 解码器 → 损失 → 评估」把 Ground3D-LMM 的存量代码读完了。本讲换一个视角:**把这个项目当作一个可扩展的框架来改造**。读完本讲,你应该能够:

1. 说出**新增一个 QA 子任务**(例如一种新的空间推理问答)时,从转换脚本、transform、评估旋钮到文本指标的**全部改动点**,并理解哪一处漏改会导致「静默失败」而不是报错。
2. 说出**接入一个全新数据集**需要实现的「三件套」——Converter(转换脚本)、Transform(超点 GT + QA 采样)、Dataset + 配置——并以仓库里现成的 ScanRefer / Reason3D 迁移代码为样板。
3. 评估**更换 LLM 底座**的可行性:`base_model` 分支在解码器里控制哪些组件、`qwen2_5_vl`(前向重写式)与 `qwen3_vl`(钩子叠加式)两条实现路线的代价差异,以及新增一个底座需要写哪些代码。
4. 找到 **LoRA 与点 token 预算的调参入口**:`lora_type/lora_r/lora_alpha` 如何决定注入范围与有效缩放,`max_point_tokens` 与四种聚合器如何控制语言侧的 token 开销。

本讲是「清单 + 样板」式的一讲:大部分结论在前面的讲义中已逐段精读过,这里把它们重新组织成**改动地图**,便于真正动手时对照执行。

## 2. 前置知识

- **改动链(changelog chain)**:一个功能在仓库里往往横跨「转换脚本 → 注册类 → 配置 → 评估工具」多层;漏掉任何一层,失败方式通常是**静默的**(数据被过滤、指标被跳过),而不是抛异常。二次开发的第一课是先把链路画出来,再动手。
- **白名单模式**:本项目的任务名、数据集名都走「硬编码名单」:
  - 转换侧的 `ALL_TASKS`(9 个任务名)决定哪些 QA 会被写进 pkl;
  - 文本指标侧的 `NUMERIC_TASKS` / `_KNOWN_TASKS` 决定哪些任务会被抽取数字、打分。
  新名字**不在名单上 = 数据被丢弃**,且不会报错。这与很多框架「见到新键就透传」的行为相反,是本项目最容易踩的坑。
- **注册机制回顾**(见 u1-l3):`uniseg3d/` 下各模块靠顶层 `@TRANSFORMS.register_module()` 等装饰器完成注册,configs 里的 `type='QA_Generation_v2'` 字符串经 `custom_imports = dict(imports=['uniseg3d'])` 触发导入后即可解析到类。**新写的 Transform / Dataset / Model 只有挂在注册表上才能被配置引用。**
- **裸文件名契约**(见 u2-l3):pkl 里只存 `points/xxx.bin` 这类裸相对文件名,完整路径由配置的 `data_prefix` 在运行时拼接。新数据集只要产出同样结构的 pkl,就能复用整套 pipeline。
- **`<p>` 数量契约**(见 u2-l4):一条 QA 的分割 GT 形状 `(num_p_tags, 超点数)` 由**答案文本里 `<p>` 标签的个数**(封顶 2)决定;没有 `<p>` 的纯文本任务自动得到全 NaN 的 GT 行,损失与指标侧都会跳过。这意味着「新增一个不需要接地的纯问答任务」几乎是零成本的。
- **LoRA 复习**(见 u4-l2):冻结底座,在每个目标线性层旁挂一对低秩矩阵 \(B A \)(\(A \in \mathbb{R}^{r \times d_{in}}, B \in \mathbb{R}^{d_{out} \times r}\)),前向变为 \(y = W_0 x + \frac{\alpha}{r} B A x\)。可训练参数量约为 \(\sum r (d_{in} + d_{out})\),缩放系数 \(\alpha/r\) 同时扮演「有效学习率放大器」。

## 3. 本讲源码地图

| 文件 | 在本讲中的角色 |
| --- | --- |
| `tools/part3d_glamm_data_utils.py` | 任务白名单 `ALL_TASKS`、QA 展平 `_flatten_qa_data`;扩展样板 `_ScanNetSceneGroundingData` / `ScanReferData` / `Reason3dData` |
| `tools/create_data.py` / `tools/create_data_scanrefer.py` | 转换入口的「分发器 + 新数据集入口」两种写法 |
| `uniseg3d/transforms_3d.py` | `QA_Generation_v2` / `QA_Generation_Test`(子任务过滤)、`get_seg_from_single_qa`(GT 生成)、ScanRefer/Reason3D 专用 Transform 样板 |
| `uniseg3d/query_decoder.py` | `base_model` 分支、`_init_reason_model`、`get_target_modules`(LoRA 入口)、四种聚合器 |
| `uniseg3d/point_qwen3_vl.py` / `uniseg3d/point_qwen2_5_vl.py` | 两种底座接入路线的对照 |
| `configs/ground3dlmm_train.py` | 所有扩展旋钮的配置侧落点(`qwen_model_path`、`lora_*`、`base_model`、`max_point_tokens`) |
| `tools/evaluate_text_metrics.py` | 文本指标侧的第二份任务名单 |

## 4. 核心概念与源码讲解

### 4.1 新增子任务的改动链

#### 4.1.1 概念说明

Ground3D 把子任务(如 `scale_estimation`、`functional_part_grounding`)组织成一个**三层字典**:`task_level`(part/object/multi_turn)→ `task_type`(qa_data/qa_data_seg)→ `sub_task`(具体任务名)。一个子任务名要在系统里「活着」,必须依次通过四道关卡:

1. **转换关卡**:QA JSON 里的任务名必须在 `ALL_TASKS` 白名单里,否则整条 QA 在生成 pkl 时被丢弃;
2. **Transform 关卡**:`QA_Generation_v2`(训练)展平时按 `qa_keys` 分派 GT 来源,`QA_Generation_Test`(评估)按三级旋钮过滤;
3. **评估旋钮关卡**:`eval_ground3d.sh` 经 `--sub_tasks` 传入的名字必须与 pkl 里的名字**逐字符相等**;
4. **文本指标关卡**:`evaluate_text_metrics.py` 只对 `NUMERIC_TASKS` 里的 4 个任务做数字抽取与 APE/δ 计算,`_KNOWN_TASKS` 之外的目录名无法回推出任务标签。

#### 4.1.2 核心流程

新增一个子任务(假设叫 `color_appearance_qa`,纯文本问答、无接地)的改动链:

```text
① 产出 QA 数据
   原始 JSON: {条目id: {"color_appearance_qa": [{question, answer}, ...]}}
        │
② 转换关卡: tools/part3d_glamm_data_utils.py
   ALL_TASKS += 'color_appearance_qa'        ← 不加 = 整条被静默丢弃
   _flatten_qa_data 按 ALL_TASKS 过滤并补 <SEG>
        │
③ pkl: qa_data['part_qa_data'/'object_qa_data'][条目]['color_appearance_qa'] = [...]
        │
④ Transform 关卡(训练): QA_Generation_v2.flatten_qa_data 自动展开
   答案无 <p> → count_p_tags=0 → GT 自动为 (1, 超点数) 的 NaN 行(零额外代码)
        │
⑤ 评估旋钮: --cfg-options pipeline.2.transforms.2.sub_task=color_appearance_qa
   (QA_Generation_Test 严格匹配;拼错 = 空跑不报错)
        │
⑥ 文本指标: 若属于数值型任务,需登记进 NUMERIC_TASKS 并写抽取规则;
   否则登记进 _KNOWN_TASKS 即可参与 judge 打分
```

#### 4.1.3 源码精读

**关卡 ①:白名单定义。** 9 个任务名是全局权威,新增任务名必须先加在这里:

- [tools/part3d_glamm_data_utils.py:34-39](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/part3d_glamm_data_utils.py#L34-L39) —— `ALL_TASKS` 常量,列出全部合法子任务名。

**关卡 ②:白名单过滤(静默丢弃点)。** 展平原始 QA JSON 时,`if task not in ALL_TASKS: continue` 一行决定了「新任务名不登记就等于不存在」:

- [tools/part3d_glamm_data_utils.py:42-65](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/part3d_glamm_data_utils.py#L42-L65) —— `_flatten_qa_data` 把 `{id: {task: [...]}}` 压成 `{task: [...]}`,并在 L54 过滤未知任务、L59-60 给问题补 `<SEG>` 占位符。

**关卡 ③:GT 分派靠 `qa_keys` 标签。** `get_seg_from_single_qa` 并不看任务名,而是看展平时打上的 `qa_keys`(`object` / `part` / `multi_turn`)决定查哪张超点 mask 表、用哪个 id 字段:

- [uniseg3d/transforms_3d.py:279-311](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/transforms_3d.py#L279-L311) —— `qa_type = single_qa['qa_keys']` 后按 `object`/`part`/`multi_turn` 三分支选取 `gt_sp_object_masks` 或 `gt_sp_part_masks`;**注意 `part` 分支取的是 `object_id` 字段**(源码注释说明原始数据只带 object_id),这是新任务标注字段要对齐的隐含约定。

**关卡 ④:无 `<p>` 任务零成本。** 纯文本任务的 GT 自动成为 NaN 哨兵:

- [uniseg3d/transforms_3d.py:406-414](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/transforms_3d.py#L406-L414) —— `count_p_tags` 统计答案中 `<p>` 个数(封顶 2)。
- [uniseg3d/transforms_3d.py:272-274](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/transforms_3d.py#L272-L274) —— `num_p_tags == 0` 时直接返回 `(1, num_superpoints)` 的全 NaN mask,后续损失与指标按 NaN 行跳过,无需为新任务写任何分割侧代码。

**关卡 ⑤:评估三级旋钮(严格匹配)。**

- [uniseg3d/transforms_3d.py:551-558](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/transforms_3d.py#L551-L558) —— `QA_Generation_Test.__init__` 接收 `task_level` / `task_type` / `sub_task` 三个过滤参数,`None` 表示该层不过滤。
- [uniseg3d/transforms_3d.py:606-619](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/transforms_3d.py#L606-L619) —— `sub_task` 层的严格过滤:不在键里就得到空列表,不抛错。

**关卡 ⑥:文本指标的第二份名单。**

- [tools/evaluate_text_metrics.py:45-50](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/evaluate_text_metrics.py#L45-L50) —— `NUMERIC_TASKS` 只有 4 个数值型任务享受 LLM 数字抽取 + APE/δ 计算。
- [tools/evaluate_text_metrics.py:83-94](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/evaluate_text_metrics.py#L83-L94) —— `_KNOWN_TASKS` 是路径回推任务标签的依据,新任务名不登记则样本可能拿不到 `_task` 标签而被后续阶段忽略。

#### 4.1.4 代码实践

**实践目标**:不写一行真实代码,产出一份数学上可核对的「新增子任务改动清单」,并用 grep 验证每个改动点的行号。

**操作步骤**:

1. 在仓库根目录执行 `grep -rn "scale_estimation" tools/ uniseg3d/ configs/ | grep -v ".pyc"`,观察这个现成任务名在四道关卡中的全部出现位置;
2. 对 `grep -rn "ALL_TASKS\|NUMERIC_TASKS\|_KNOWN_TASKS" tools/` 做第二次搜索,确认两份名单的全部引用点;
3. 假设新增任务 `color_appearance_qa`(纯文本、无 `<p>`),写下一张四行表格:改动文件、行号、改动内容、漏改后果。

**需要观察的现象**:`scale_estimation` 会同时命中 `tools/part3d_glamm_data_utils.py`(白名单)、`tools/evaluate_text_metrics.py`(NUMERIC_TASKS 与抽取规则)、`eval_ground3d.sh` 相关文档;而 `configs/` 目录里**不会**出现任务名——因为评估任务选择靠 `--cfg-options` 运行时注入,不写死在配置文件里。

**预期结果**:得到类似下表的清单(行号以当前 HEAD 为准):

| 关卡 | 文件:行号 | 改动 | 漏改后果 |
| --- | --- | --- | --- |
| 转换 | `tools/part3d_glamm_data_utils.py:34` | `ALL_TASKS` 追加任务名 | QA 全部被静默丢弃,pkl 里查无此任务 |
| 训练采样 | 无需改动 | `QA_Generation_v2` 自动展开 | —— |
| GT 生成 | 无需改动(无 `<p>` 时) | NaN 哨兵自动生效 | —— |
| 评估选择 | 无需改代码,`--cfg-options` 传 `sub_task` | —— | 拼错则空跑,不报错 |
| 文本指标 | `tools/evaluate_text_metrics.py:45/83` | 登记进名单(+数值任务写抽取规则) | 样本丢失 `_task` 标签,指标被跳过 |

本实践为源码检索型,`grep` 命令本身不依赖运行环境;清单行号若在你本地 HEAD 已漂移,以 grep 结果为准。

#### 4.1.5 小练习与答案

**练习 1**:如果一个新任务的答案里含有 3 个 `<p>` 标签,GT 形状会是几行?为什么?

**答案**:`count_p_tags` 把计数封顶在 2,所以 GT 是 `(2, num_superpoints)`,且由于 `get_seg_from_single_qa` 末尾「行数与 `num_p_tags` 不符则整条置 NaN」的对齐保护(L393-403),第 3 个指称不会造成错位——最坏情况是整条 QA 的 GT 退化为 NaN 而被损失跳过。设计新任务时**答案最多写 2 个 `<p>`**。

**练习 2**:`QA_Generation_Test` 的 `sub_task` 传了一个 pkl 中不存在的任务名,会发生什么?如何提前发现?

**答案**:`flatten_qa_data` 的三层过滤得到空 `qa_list`,`transform` 产出空的 `selected_qa_data`,评估循环体一次都不执行、不报错(见 u6-l2 的「静默空跑」)。提前发现的办法:用 `--cfg-options` 干跑前先 `python -c "import pickle; d=pickle.load(open('part3d_glamm_infos_val.pkl','rb')); print(d[0]['qa_data'].keys())"` 核对 pkl 中真实存在的层级与任务名(待本地验证,依赖本地已生成 pkl)。

**练习 3**:为什么 `get_seg_from_single_qa` 里 `part` 分支取的是 `single_qa['object_id']` 而不是 `part_id`?

**答案**:源码 L289 注释明确说明原始 Ground3D 数据的 part 级 QA 只携带 `object_id` 字段(部件在其所属物体的 mask 表 `gt_sp_part_masks` 中按 object_id 索引);只有 `multi_turn` 分支才同时检查 `object_id` 与 `part_id` 两个字段(L293-301)。给新任务写标注时,字段名必须沿用这一约定,否则会落入「id 不在参照表 → NaN」的兜底分支。

### 4.2 新增数据集的三件套

#### 4.2.1 概念说明

「接入一个新数据集」在本仓库有一个已经被 ScanRefer / Reason3D 验证过的**三件套**模板(u7-l1 逐段读过,这里抽象成方法论):

1. **Converter(转换脚本)**:把外部标注转成 OpenMMLab v2 格式的 infos pkl——决定磁盘上有什么、pkl 里写什么;
2. **Transform(数据变换)**:两个注册类——超点级 GT 生成器(把逐点 mask 聚合成超点表)+ QA 采样器(把 `qa_data` 变成 `selected_qa_data` 与 `sp_gt_seg`);
3. **Dataset + 配置**:一个数据集类(通常直接复用 `Part3DGlamm_Dataset`)+ 一份 eval 配置(必要时换解码器变体)。

三件套的关键在于**契约而非代码量**:pkl 的字段名、`data_prefix` 的目录名、`qa_keys` 标签、`<p>` 数量契约,这四样对齐之后,模型侧一行代码都不用改。

#### 4.2.2 核心流程

```text`
外部数据(点云 + 超点 + mask + QA JSON)
   │
   ▼ ① Converter:写 tools/create_data_<name>.py + <Name>Data 类
      继承 _ScanNetSceneGroundingData → 只需覆盖 2 个成员:
        qa_json_template = '<标注文件名模板>.json'
        _instance_mask(idx, reason_data) → (N,) int 实例标签
      产出 <prefix>_infos_{train,val}.pkl(裸文件名 + qa_data 整段)
   │
   ▼ ② Transform:在 uniseg3d/transforms_3d.py 写两个 @TRANSFORMS.register_module() 类
      AddSuperPointAnnotations_<Name>   → gt_sp_masks_ins / gt_sp_masks_sem
      QA_Generation_<Name>_Test         → selected_qa_data + sp_gt_seg(行数=<p>数)
   │
   ▼ ③ Dataset + 配置
      复用 Part3DGlamm_Dataset(裸文件名契约天然兼容)
      新建 configs/ground3dlmm_eval_<name>.py:
        换 pipeline 中的两个 Transform、换 ann_file/data_prefix、
        按是否带图像选 Grounded_Decoder_Eval_Text / _MultiImg
```

#### 4.2.3 源码精读

**① Converter 的「两个成员」极简继承。** ScanRefer 与 Reason3D 的转换类各自只定义了一个文件名模板和一个实例 mask 取数函数,其余(扫描场景、写 bin、拼 qa_data、v2 规范化)全部继承自共享基类:

- [tools/part3d_glamm_data_utils.py:410-425](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/part3d_glamm_data_utils.py#L410-L425) —— `ScanReferData`:`qa_json_template` + `_instance_mask`(从 `<scene>_reason.pth` 的第 5 个张量取实例标签)。
- [tools/part3d_glamm_data_utils.py:428-442](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/part3d_glamm_data_utils.py#L428-L442) —— `Reason3dData`:同样两个成员,实例标签换成 `<scene>_ins_label.npy`。**两个数据集的转换器差异只有约 5 行**,这就是「继承共享基类」的扩展收益。
- [tools/part3d_glamm_data_utils.py:244-258](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/part3d_glamm_data_utils.py#L244-L258) —— 共享基类 `_ScanNetSceneGroundingData` 的定位:读预处理 ScanNet 数据 + 逐场景 QA 标注,产出整场景 infos。

**①' 转换入口的两种写法。** 主转换器用 `--dataset` 分发,新数据集则单开脚本(更清晰、避免改主入口):

- [tools/create_data.py:107-125](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/create_data.py#L107-L125) —— 主入口按 `args.dataset` 分发,未知名直接 `raise NotImplementedError`——这是「显式失败」风格的正面样板,与白名单的「静默过滤」形成对照。
- [tools/create_data_scanrefer.py:18-49](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/create_data_scanrefer.py#L18-L49) —— 新数据集入口 `scanrefer_data_prep`:构造 `ScanReferData` → `get_infos` → `mmengine.dump` → `update_pkl_infos('reason3d', ...)` 借用 reason3d 的整场景 v2 规范化例程(L47-49 注释解释了为何借用的原因)。

**② Transform 样板一:超点 GT 生成。**

- [uniseg3d/transforms_3d.py:1436-1453](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/transforms_3d.py#L1436-L1453) —— `AddSuperPointAnnotations_ScanRefer` 的注册与签名:与 Ground3D 主版本(`AddSuperPointAnnotations_Part3DGlamm`,L1265 起)平行的独立类,而非参数化复用——**新增数据集倾向于拷贝改,而不是加开关**。

**② Transform 样板二:QA 采样(自闭环式)。** ScanRefer 的 QA 采样器不走 `get_seg_from_single_qa` 的 `qa_keys` 分派,而是自己从语义表直接取行——展示了「绕开通用机制、自建闭环」的第二条路线:

- [uniseg3d/transforms_3d.py:1660-1712](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/transforms_3d.py#L1660-L1712) —— `QA_Generation_ScanRefer_Test.transform`:把描述原句回显为答案(`'<p>'+qa['description']+'</p><SEG>'`,L1685)、打上 `qa_keys='scanrefer'` 等标签、直接 `gt_sp_masks_ins[object_id]` 取 GT 行(L1697),取不到则 NaN 兜底(L1700-1704)。
- [uniseg3d/transforms_3d.py:834-873](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/transforms_3d.py#L834-L873) —— `QA_Generation_Reason3D_Test` 同构,GT 改从 `gt_sp_masks_sem` 语义表取行(L862)。

**③ 配置侧的落点。** 参见 u7-l1 精读过的 `configs/ground3dlmm_eval_scanrefer.py` 等 4 份配置:核心改动就是 pipeline 里替换上述两个 Transform、`ann_file`/`data_prefix` 指向新 pkl 与新目录、解码器按带图与否在 `Grounded_Decoder_Eval_Text` 与 `Grounded_Decoder_Eval_MultiImg` 间选择(两者定义见 [uniseg3d/query_decoder.py:3152-3160](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L3152-L3160) 与 [uniseg3d/query_decoder.py:3273-3281](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L3273-L3281))。

#### 4.2.4 代码实践

**实践目标**:为假想数据集 `MyRooms`(整场景、实例级指称分割,标注为逐场景 JSON)写出 Converter 子类**草稿**与三件套文件清单,训练「按样板映射」的能力。

**操作步骤**:

1. 重读 [tools/part3d_glamm_data_utils.py:410-442](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/part3d_glamm_data_utils.py#L410-L442) 两个样板类,确认子类化只涉及 `qa_json_template` 与 `_instance_mask`;
2. 写出下面的草稿(**示例代码,不是项目原有代码,不要写入仓库**);

```python
# 示例代码:tools/part3d_glamm_data_utils.py 追加(草稿,未运行)
class MyRoomsData(_ScanNetSceneGroundingData):
    """Whole-scene MyRooms grounding dataset."""
    qa_json_template = 'myrooms_{split}_by_scene.json'

    def _instance_mask(self, idx, reason_data):
        # 与 ScanReferData 相同:实例标签取 reason.pth 的第 5 个张量
        import torch
        instance_labels = reason_data[4]
        if isinstance(instance_labels, torch.Tensor):
            instance_labels = instance_labels.numpy()
        return np.asarray(instance_labels)
```

3. 对照 `QA_Generation_ScanRefer_Test`(L1653-1712)列出新数据集 Transform 需要改的 5 处:question 模板、answer 模板、`qa_keys` 标签、GT 表名(`gt_sp_masks_ins` 或 `_sem`)、NaN 兜底分支;
4. 列出三件套文件清单:`tools/create_data_myrooms.py`(照抄 create_data_scanrefer.py 的 `scanrefer_data_prep` 骨架)、`uniseg3d/transforms_3d.py` 追加两个注册类、`configs/ground3dlmm_eval_myrooms.py`(以 eval_scanrefer 配置为 `_base_` 覆盖 `ann_file` 与 pipeline 两个 Transform)。

**需要观察的现象**:草稿与 `ScanReferData` 逐行 diff 后,唯一实质差异是 `qa_json_template`;Transform 清单里 GT 表名的选择对应「实例级(ScanRefer 用 `_ins`)还是语义级(Reason3D 用 `_sem`)」这一唯一建模决策。

**预期结果**:得到一份约 20 行的改动文件清单。**本实践为源码阅读 + 草稿设计型,草稿未运行,待本地验证**(验证方式:准备一个只含 1 个场景的迷你标注,跑 `create_data_myrooms.py` 看能否产出 pkl,再用 u1-l4 的干跑方式核对配置能否解析)。

#### 4.2.5 小练习与答案

**练习 1**:为什么 `ScanReferData` 不需要重写 `get_infos`、`_process_qa` 这些真正干活的函数?

**答案**:因为共享基类 `_ScanNetSceneGroundingData` 已经固化了「扫描场景 → 读点云/超点/实例标签 → 写裸文件名 bin → QA 整段塞进 `qa_data`」的流程,数据集间的差异被压缩到「标注文件叫什么、实例标签从哪读」两点,模板方法模式把变化点收敛为两个钩子。新数据集只要外部数据能摆成基类期望的目录结构(`scannet_instance_data/`、`<scene>_reason.pth`、逐场景 QA JSON),继承即可。

**练习 2**:如果新数据集的超点粒度比 Ground3D 细很多(超点数常年超过 5000),三件套之外还要动什么?

**答案**:要动训练侧的点 token 预算——`max_point_tokens`(见 4.4)与解码器 6 层注意力的序列长度。超过预算的训练样本会被 FPS 聚合到 `max_point_tokens`,信息被有损压缩;若想保留更多超点,需同时上调 `max_point_tokens` 并接受显存与序列长度的线性开销(仅语言侧;分割解码器的 K/V 始终用全量超点,不受该参数影响,见 u4-l3)。

### 4.3 更换 LLM 底座(base_model 分支)

#### 4.3.1 概念说明

解码器把「用哪个语言底座」抽象成构造参数 `base_model`,目前支持 `'qwen2_5_vl'` 与 `'qwen3_vl'` 两个取值(训练配置里用的是后者)。这个字符串在解码器内部触发**五个分支点**:模型类、config 类、processor、词表大小的取数路径、`point2token` 投影桥的结构。更重要的是,仓库里存在**两条接入路线**:

- **前向重写式**(`point_qwen2_5_vl.py`):整体覆写 `forward`,显式处理图像/视频/点三种模态的 `masked_scatter`,并直接弃用视觉塔(`self.visual = None`);用 `training/caching/generating` 三态判断决定何时注入点特征。
- **钩子叠加式**(`point_qwen3_vl.py`):完全不覆写前向逻辑,只在 `language_model` 上挂一个 forward pre-hook(`add_point_embedding`),在嵌入进入语言塔之前完成 `masked_scatter`;视觉塔原样保留。

评估替换底座的可行性,本质是评估「为 新底座写一个 `point_xxx.py` + 在五个分支点各加一个 elif」的工作量与风险。

#### 4.3.2 核心流程

替换/新增底座时被 `base_model` 字符串驱动的分支全景:

```text
config: decoder = dict(base_model='qwen3_vl', qwen_model_path='Qwen/Qwen3-VL-4B-Instruct', ...)
          │
          ▼
Grounded_Decoder_Joint.__init__
  ├─ 分支 1  _init_reason_model: 选模型类
  │     qwen2_5_vl → PointQwen2_5_VLForConditionalGeneration(前向重写式)
  │     qwen3_vl   → PointQwen3VLForConditionalGeneration(钩子式)
  ├─ 分支 2  词表大小取数路径
  │     qwen2_5_vl → config.vocab_size(顶层)
  │     qwen3_vl   → config.text_config.vocab_size(嵌套)
  ├─ 分支 3  point2token 结构
  │     qwen2_5_vl → RMSNorm → Linear(256→hidden)            两段
  │     qwen3_vl   → RMSNorm → Linear(256→256) → GELU → Linear(256→hidden)  四段
  ├─ 分支 4  get_target_modules: 两个底座共用同一 qkvo 键名单(含视觉塔 attn.qkv/attn.proj)
  └─ 分支 5  preprocess_chatml / get_conv('chatml', qwen_model=base_model):
         按底座选 ChatML 系统提示与分隔符约定
```

#### 4.3.3 源码精读

**分支总开关与默认值。** 注意构造器的默认底座是 `qwen2_5_vl`,而训练配置显式覆盖为 `qwen3_vl`——读配置时不要被默认值误导:

- [uniseg3d/query_decoder.py:884-899](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L884-L899) —— 构造器签名:`base_model='qwen2_5_vl'`(L894)、`max_point_tokens=2500`(L895,配置覆盖为 2000)。
- [configs/ground3dlmm_train.py:68-76](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_train.py#L68-L76) —— 配置侧落点:`qwen_model_path='Qwen/Qwen3-VL-4B-Instruct'`、`lora_type='qkvo_all'`、`lora_r=16`、`base_model='qwen3_vl'`、`max_point_tokens=2000`。

**分支 1+2:`_init_reason_model` 的三路 if。**

- [uniseg3d/query_decoder.py:959-976](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L959-L976) —— `qwen2_5_vl` 走 `PointQwen2_5_VLForConditionalGeneration` + 顶层 `config.vocab_size`;`qwen3_vl` 走 `PointQwen3VLForConditionalGeneration` + `config.text_config.vocab_size`;其余值抛 `ValueError`。**新增底座的第一件事就是在这里加 elif。**

**分支 3:`point2token` 投影桥按底座变形。**

- [uniseg3d/query_decoder.py:923-947](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L923-L947) —— `qwen2_5_vl` 分支两段式(L928-930),`qwen3_vl` 分支四段式(L937-939);`query_proj`(L944-947)则**不分底座、恒用 `Qwen3VLTextRMSNorm`**——因为它的输入永远来自当前底座的最后一层隐状态,输出永远降到 `d_model=256`,两端维度由 `reason_mode_config` 决定,归一化层类型沿用 Qwen3 实现即可。

**两条接入路线对照(替换底座的核心参考)。**

- [uniseg3d/point_qwen3_vl.py:21-49](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/point_qwen3_vl.py#L21-L49) —— 钩子式:校验「`point_mask` 为 True 的个数 == `point_embeds` 行数」的数量契约后 `masked_scatter`,返回 `((), kwargs)` 替换模块输入。
- [uniseg3d/point_qwen3_vl.py:70-82](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/point_qwen3_vl.py#L70-L82) —— 子类的全部「定制」就是挂两个钩子(存隐状态 + 注点嵌入)加重建 `lm_head`,其余交给 `super().forward`。
- [uniseg3d/point_qwen3_vl.py:146-151](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/point_qwen3_vl.py#L146-L151) —— 新底座接入 transformers 的「注册三连」:`MODEL_FOR_VISION_2_SEQ_MAPPING_NAMES` + `AutoConfig.register` + `AutoProcessor.register`。
- [uniseg3d/point_qwen2_5_vl.py:37-46](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/point_qwen2_5_vl.py#L37-L46) —— 重写式:`PointQwen2_5_VLModel` 直接 `self.visual = None`(L42 注释:视觉塔暂不使用),语言塔单独重建。
- [uniseg3d/point_qwen2_5_vl.py:60-137](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/point_qwen2_5_vl.py#L60-L137) —— 重写式 `forward`:先算 `mode`(training/caching/generating,L84),再依次处理图像(L92-105)、视频(L107-120)、点(L122-137)三种 `masked_scatter`;点注入只在 `training/caching` 两态生效、`generating` 态跳过——**这意味着 qwen2_5_vl 路线天然无法与 Joint(3D+2D)变体配合**(视觉塔已弃),只能做 3D-only。

**可行性结论**:新增底座(例如换更大的 Qwen3-VL 或其他家族)时,推荐照抄 `point_qwen3_vl.py` 的钩子式模板——零前向重写、保留视觉塔、与 Joint 变体兼容;需要新写的只有:① 一个 `point_<name>.py`(子类 + 两个钩子 + 注册三连);② `_init_reason_model`/`__init__` 各加一个 elif;③ `get_target_modules` 的键名单按新底座的注意力投影层名核对;④ `get_conv('chatml', ...)` 侧若新底座的 ChatML 约定不同需在 `uniseg3d/conversation.py` 补模板(待确认:`conversation.py` 未在本讲精读,改动前先读它)。风险点:LoRA rank 等超参不随底座自动迁移,且 checkpoint 与配置的 rank 失配会被静默吞掉(u6-l2 的 `probe_lora_rank` 教训)。

#### 4.3.4 代码实践

**实践目标**:用「零 GPU 配置干跑」验证 `base_model` 分支的真实影响面——只改一个字符串,看配置解析后的组件差异。

**操作步骤**:

1. 在能 import 本项目的环境里(见 u1-l2),执行下面的检查脚本(**示例代码**);

```python
# 示例代码:check_base_model.py(放于仓库外运行,不写入仓库)
from mmengine.config import Config
cfg = Config.fromfile('configs/ground3dlmm_train.py')
d = cfg.model.decoder
print('base_model    =', d['base_model'])
print('qwen_model_path =', d['qwen_model_path'])
print('point2token 输入维度 =', d['d_model'])
print('max_point_tokens =', d['max_point_tokens'])
print('lora =', d['lora_type'], d['lora_r'], d['lora_alpha'])
```

2. 把 `d['base_model']` 换成 `'qwen2_5_vl'` 再打印一次,对照 4.3.2 的分支全景图,口头推演哪些组件会变(模型类、词表取数路径、`point2token` 段数);
3. 再试一个非法值 `base_model='qwen4_vl'`,读配置仍会成功——因为 `ValueError` 只在**构建模型实例时**抛出([uniseg3d/query_decoder.py:975-976](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L975-L976));体会「配置层静默、构建层报错」的分层。

**需要观察的现象**:步骤 1 正常打印 5 行;步骤 3 说明拼错底座名在 `Config.fromfile` 阶段毫无反应,只有真正 `MODELS.build` 时才炸——这解释了为什么推荐用干跑脚本分层验证。

**预期结果**:能列出 `qwen2_5_vl` 与 `qwen3_vl` 在组件上的三点差异清单。本实践只做配置解析、不加载权重,**待本地验证**(需要 u1-l2 的环境;不构建模型则无需 GPU)。

#### 4.3.5 小练习与答案

**练习 1**:`query_proj` 为什么不需要按 `base_model` 分支,而 `point2token` 需要?

**答案**:`point2token` 是「3D 侧 256 维 → 底座 hidden_size」的入口桥,两个底座的 hidden_size 与作者选择的深度不同(Qwen2.5-VL-7B 与 Qwen3-VL-4B 的隐层宽度和层数不同),所以结构跟着底座走;`query_proj` 是「底座 hidden_size → 256 维」的出口桥,它的输入永远是对齐后的 `<SEG>` 隐状态、输出永远喂给 256 维的分割解码器,两端接口与底座解耦,只需从 `reason_mode_config.text_config.hidden_size` 读对输入维度即可(L944-947)。

**练习 2**:如果用 `base_model='qwen2_5_vl'` 训练,能否同时使用 RGB 图像分支(Joint 变体)?为什么?

**答案**:不能。`PointQwen2_5_VLModel.__init__` 里 `self.visual = None` 弃用了视觉塔,`forward` 里对 `pixel_values` 的处理依赖 `get_image_features`(内部走 visual 塔),图像输入将不可用;而 Joint 变体的 message 里带 image 条目、processor 会产出 `pixel_values`。这正是 u4-l1 的结论:钩子叠加式(qwen3_vl)是 Joint 变体的唯一基础。

**练习 3**:新底座的注意力投影层名如果不叫 `q_proj/k_proj/v_proj/o_proj`,LoRA 注入会怎样?

**答案**:`get_target_modules` 用 `n.endswith(k)` 匹配硬编码的 `qkvo_keys`([uniseg3d/query_decoder.py:1029-1043](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1029-L1043)),一个都匹配不到时 `target_modules` 为空,peft 不会注入任何 LoRA——训练退化成只更新 `embed_tokens/lm_head`。失败同样是静默的;防御办法是看训练日志里 `LoRA target modules: [...]` 那一行(L999)是否非空。

### 4.4 LoRA 与聚合器的调参入口

#### 4.4.1 概念说明

这一节把「不需要写新类、只改配置数字」的扩展点收拢成一张调参地图:

- **LoRA 四旋钮**:`lora_type`(注入哪些层)、`lora_r`(秩)、`lora_alpha`(缩放)、`lora_dropout`/`lora_bias`;
- **点 token 预算**:`max_point_tokens`(语言侧超点 token 上限)+ 聚合器选择;
- **四种聚合器**:仓库实现了四条「变长超点 → 定长 token」的路线,其中两种零参数(FPS 系)、两种带学习参数(conv / 软聚类),但解码器当前只实例化了 FPS 系两种。

#### 4.4.2 核心流程

LoRA 注入与超预算聚合在训练前向中的位置:

```text
训练 step(单场景)
  extract_feat: SpConvUNet → scatter_mean → 逐超点 256 维特征 (M, 256)
       │
       ├─ (分割支路) 全量 M 个超点 → 6 层查询解码器的 K/V,不受任何预算限制
       │
       └─ (语言支路) M > max_point_tokens ?
              是 → pos_aggregator(质心 FPS, 首选) 或 point_aggregator(特征 FPS, 兜底)
                    压到 (max_point_tokens, 256)
              否 → 原样通过
            point2token 升维 → <|point|> 占位 masked_scatter 注入底座
            底座内部:每个目标线性层 y = W₀x + (α/r)·BAx   ← LoRA 旁路
```

每个被注入线性层的 LoRA 参数量为 \( r(d_{in} + d_{out}) \),前向缩放系数为 \( \alpha / r \)。Qwen3-VL-4B 语言塔的 q/k/v/o 投影均为方阵(\(d \times d\), \(d=2560\)),故每层每投影增加 \(2rd\) 参数;若某层投影维度不同,以 `named_modules` 的实际形状为准(纸面估算,待本地验证)。

#### 4.4.3 源码精读

**LoRA 四旋钮的配置落点与默认值差异。** 构造器默认 `lora_r=128, lora_alpha=256`,发布配置用 `r=16, α=32`——注意两者 \(\alpha/r\) 均为 2,缩放不变、容量减半:

- [uniseg3d/query_decoder.py:889-894](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L889-L894) —— 构造器默认值(`lora_type='qkvo_all'`, `lora_r=128`, `lora_alpha=256`)。
- [configs/ground3dlmm_train.py:69-74](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_train.py#L69-L74) —— 训练配置:`lora_type='qkvo_all'`、`lora_r=16`、`lora_alpha=32`、`lora_dropout=0.1`、`lora_bias='none'`;L70 注释里列出了更细粒度的可选投影名。

**`lora_type` 的两段式语法。** `层型_范围`:层型 ∈ (qkvo, linear),范围 ∈ (llm, visual, all):

- [uniseg3d/query_decoder.py:1025-1047](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1025-L1047) —— `lora_type.split('_')` 后:范围段做包含过滤(`llm` 排除 visual、`visual` 只留 visual),层型段用 `qkvo_keys`(含视觉塔的 `attn.qkv`/`attn.proj`)做后缀匹配。`'qkvo_all'` = 语言塔+视觉塔的全部注意力投影。注释提醒:当前发布配置与注入后的二次修剪配合,实际生效的是语言塔(u4-l2 的结论——`visual` 全冻)。

**注入后的参数统计(调参时的自检出口)。**

- [uniseg3d/query_decoder.py:998-1021](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L998-L1021) —— `get_peft_model` 注入(L1008)→ 解冻 `embed_tokens/lm_head`、全冻 `visual`(L1010-1014)→ 打印总参数/可训练参数/占比(L1018-1021)。改 `lora_r` 后对照这行日志即可纸面核对增量。

**`max_point_tokens` 与两个聚合器的实例化。**

- [uniseg3d/query_decoder.py:952-955](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L952-L955) —— 同时实例化 `FPSAggregator` 与 `FPSPositionAggregator`(两者均零参数),预算由 `max_point_tokens` 决定。
- [uniseg3d/query_decoder.py:1360-1368](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1360-L1368) —— 训练时的实际选择逻辑:`self.training and n_points > self.max_point_tokens` 才聚合;有质心坐标则优先 `pos_aggregator`,否则退到 `point_aggregator`;**推理(`self.training == False`)永不聚合**。

**四种聚合器实现(替换入口)。**

- [uniseg3d/query_decoder.py:87-118](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L87-L118) —— `ConvSetAggregator`:1D 卷积 + 自适应平均池化,**带学习参数**,且池化会混合相邻超点特征(顺序敏感)。
- [uniseg3d/query_decoder.py:120-159](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L120-L159) —— `PositionSoftClusterAggregator`:可学习质心 + 距离 softmax 软分配,**带学习参数**,置换不变。
- [uniseg3d/query_decoder.py:162-222](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L162-L222) —— `FPSAggregator`:按**特征 L2 距离**最远点采样,零参数,逐行选择(不混合特征)。
- [uniseg3d/query_decoder.py:225-289](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L225-L289) —— `FPSPositionAggregator`:按**质心空间距离** FPS,零参数;两者 FPS 主循环均先算 `torch.cdist` 全量距离矩阵 \(O(N^2)\) 显存,大场景下这是聚合本身的成本。

**调参速查表**(改动均在 `configs/ground3dlmm_train.py` 的 `model.decoder` 内):

| 旋钮 | 现值 | 调大的影响 | 调小的影响 |
| --- | --- | --- | --- |
| `max_point_tokens` | 2000 | 语言侧信息更全;序列更长、显存↑;超点多的场景更少被压缩 | 更多场景触发 FPS 有损压缩 |
| `lora_r` | 16 | 容量↑、可训练参数≈线性↑、易过拟合 | 欠拟合风险 |
| `lora_alpha` | 32 | 等比放大 LoRA 输出(有效学习率↑) | 旁路贡献变小 |
| `lora_type` | `qkvo_all` | 改成 `qkvo_llm` 可缩小注入面(视觉塔本就被冻) | `..._visual` 只注视觉塔,与发布训练目标不符 |
| 聚合器 | pos-FPS 首选 | 换 `ConvSetAggregator`/`PositionSoftClusterAggregator` 需改 `__init__` 两行 + 处理可学习参数的保存加载 | —— |

#### 4.4.4 代码实践

**实践目标**:纸面推导 + 日志核对,量化「`lora_r` 从 16 提到 64」的参数代价,并确认聚合触发条件。

**操作步骤**:

1. **纸面计算**:Qwen3-VL-4B 语言塔 hidden size \(d=2560\)(以你拉取的模型 `config.text_config.hidden_size` 为准)、注意力层数 \(L\)(以 `config.text_config.num_hidden_layers` 为准)。每个 q/k/v/o 投影是 \(d \times d\) 线性层,LoRA 增量为 \(r(d_{in}+d_{out}) = 2rd\);全部注入层的总增量:

\[
\Delta_{\text{LoRA}} = 4 \times L \times 2 r d = 8 L r d
\]

   代入 \(r=16\) 与 \(r=64\) 分别求差;
2. **日志核对**:找一个已跑过的训练日志(或重新 dry-run),搜索 `LoRA target modules` 与 `Total params ... Learnable params ...` 两行([uniseg3d/query_decoder.py:999](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L999) 与 [L1021](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1021)),数一下 target 列表长度是否等于 \(4L\),Learnable 增量是否与纸面一致(注意 Learnable 还包含解冻的 embed/lm_head);
3. **聚合触发观察**:在 [uniseg3d/query_decoder.py:1367](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1367) 下方那行被注释掉的 `MMLogger...info` 日志恢复(思想实验:如果取消注释),跑一个超点数 > 2000 的场景,应看到 `aggregate points from M to 2000 via pos-based FPS aggregator`。

**需要观察的现象**:步骤 2 中 target 列表条数、纸面与日志两个数字的吻合度;步骤 3(若执行)中「质心可用走 pos 分支」的选择路径。

**预期结果**:\(\Delta\) 随 \(r\) 线性增长,占比仍远小于解冻词嵌入的规模(u4-l2 结论:约 4 亿可训练参数的大头是嵌入层)。**待本地验证**:层数 \(L\)、hidden size 以实际拉取的模型 config 为准;步骤 3 需要修改源码注释,建议只在本地实验分支做,不要提交。

#### 4.4.5 小练习与答案

**练习 1**:为什么推理时「永不聚合」是安全的,而训练时必须聚合?

**答案**:训练时 batch 内场景超点数不定,`point2token` 后的点 token 数必须与 `<|point|>` 占位个数严格相等(u4-l1 的数量契约),聚合是控制**序列长度/显存**的手段;推理时单场景逐条处理,超点数就是模型构建时写进 message 的占位个数,天然自洽——只是序列长、推理慢,不存在契约破裂。代价是推理显存随场景超点数无上限增长。

**练习 2**:`lora_alpha` 从 32 改成 64,保持 `lora_r=16`,训练动力学上等于什么?

**答案**:缩放系数 \(\alpha/r\) 从 2 变 4,等价于把 LoRA 旁路输出的有效学习率放大一倍(参数量不变)。常见调法是固定 \(\alpha/r\) 比值、只动 \(r\);发布配置(16/32)与构造器默认(128/256)的 \(\alpha/r\) 同为 2,就是这个惯例的体现。

**练习 3**:四种聚合器里哪两种「不混合特征」?为什么本项目默认选它们?

**答案**:`FPSAggregator` 与 `FPSPositionAggregator` 只做**逐行选择**(`x[indices]`),不改变被选中超点的特征向量;ConvSet / SoftCluster 会把多个超点的特征加权合成新 token。选择式聚合保留了「一个 token 对应一个真实超点」的语义,使注入底座的 `<|point|>` token 与分割解码器的 K/V 超点之间保持可解释的对应;且零参数、不引入需保存的权重,与冻结底座 + LoRA 的轻量微调哲学一致。

## 5. 综合实践

**任务:为「新数据集 + 新子任务」的组合扩展写一份完整的落地清单(二次开发 checklist)。**

设定:你要把 Ground3D-LMM 迁移到自采数据集 `HomeScan`(整场景、带部件级标注),并新增一个子任务 `material_reasoning`(问「桌上哪个部件是金属的」,答案含 1 个 `<p>` 指称)。请产出一包含**文件级 diff 预算**的清单,覆盖:

1. **数据准备**(对照 4.2):`tools/part3d_glamm_data_utils.py` 新增 `HomeScanData(_ScanNetSceneGroundingData)`(列出要覆盖的成员)、新入口 `tools/create_data_homescan.py`(照抄 scanrefer 骨架)、若走整场景 v2 规范化借用哪个例程;
2. **新子任务登记**(对照 4.1):`ALL_TASKS` 追加、答案含 1 个 `<p>` 时 GT 形状、`qa_keys` 应打 `part`(注意 GT 查 `gt_sp_part_masks` 且标注字段用 `object_id`)、文本指标侧是否登记(`material_reasoning` 非数值型,只需进 `_KNOWN_TASKS` 参与 judge);
3. **Transform**(对照 4.2):新写 `AddSuperPointAnnotations_HomeScan` 与 `QA_Generation_HomeScan_Test` 各自的输入键/输出键契约表;
4. **配置**(对照 4.3/4.4):eval 配置继承哪份、`sub_task=material_reasoning` 如何经 `--cfg-options` 注入、`max_point_tokens` 若 HomeScan 超点偏细要调到多少、LoRA 是否沿用 `r=16/α=32`;
5. **验收**(对照 u1-l4/u6-l2):干跑配置 → 单场景推理 → 检查预测 JSON 三字段 → mIoU 与 judge 两通道是否都有产出,以及每一步「静默失败」的探测方法(空 QA 列表、无 `_task` 标签、LoRA target 为空)。

**验收标准**:清单中每个条目都能指向本讲引用过的具体文件与行号;总改动预算应收敛在「2 个转换类 + 1 个入口脚本 + 2 个 Transform 类 + 1 份配置 + 2 处名单登记」左右。若你的预算明显超出,多半是把「拷贝改」误做成了「深度重构」。

## 6. 本讲小结

- **新增子任务的改动链**有四道关卡:转换白名单 `ALL_TASKS`(漏改 = QA 静默丢失)、`qa_keys` 的 GT 分派约定(part 级也用 `object_id` 字段)、评估三级旋钮的严格匹配(拼错 = 空跑)、文本指标的第二份名单 `NUMERIC_TASKS`/`_KNOWN_TASKS`;无 `<p>` 的纯文本任务靠 NaN 哨兵几乎零成本接入。
- **新增数据集的三件套** = Converter(继承 `_ScanNetSceneGroundingData` 只覆盖 2 个成员)+ Transform(GT 生成器 + QA 采样器两个注册类)+ Dataset 与配置(复用 `Part3DGlamm_Dataset`,靠裸文件名契约共享 pipeline);ScanRefer 与 Reason3D 的转换器差异仅约 5 行,证明该模板的可复制性。
- **更换底座**由 `base_model` 字符串驱动五个分支点(模型类、词表路径、`point2token` 结构、LoRA 键名单、ChatML 模板);仓库存在前向重写式(qwen2_5_vl,弃视觉塔、只能 3D-only)与钩子叠加式(qwen3_vl,保留视觉塔、支持 Joint)两条路线,新增底座应优先照抄钩子式模板,并警惕 LoRA target 为空、rank 失配两类静默失败。
- **LoRA 与点 token 预算**是纯配置层调参入口:`lora_r` 线性控制旁路容量(参数量 \(8Lrd\) 量级)、\(\alpha/r\) 是有效缩放;`max_point_tokens` 只约束语言侧 token,训练超限时由零参数的 pos-FPS(首选)/feature-FPS 聚合压缩,推理永不聚合、分割支路的 K/V 永远用全量超点。
- 本项目的扩展哲学:**契约优先、拷贝改优先、显式与静默并存**——pkl 字段名、`qa_keys`、`<p>` 数量这些契约对齐后模型侧零改动;新数据集靠复制样板类而非加开关;同时大量「名单过滤 + strict=False 加载」意味着二次开发的错误多以静默形式出现,分层干跑验证是必修课。

## 7. 下一步学习建议

- 下一讲 **u7-l3(架构取舍与总结)** 会跳出「怎么改」去讨论「为什么这样设计」:超点 token 化的效率、`<SEG>` 隐状态复用为分割查询、LoRA 而非全参微调、生成后二次前向的精度考量——本讲的扩展点清单正好为那些取舍提供反面素材(如果当时选了另一条路,现在扩展会有多难)。
- 想继续深挖替换底座,建议通读 `uniseg3d/conversation.py`(本讲标注「待确认」的 ChatML 模板分支)与 `point_qwen3_vl.py` 的 `prepare_inputs_for_generation`(L131-143,`cache_position[0] == 0` 才传点特征的生成期注入细节)。
- 想验证自己的扩展能力,可从最小改动做起:给 `evaluate_text_metrics.py` 的 `--selftest`(L513 起的样例断言)补一条新任务的用例,跑一次自测——这是仓库里唯一一处开箱即用的单元测试式入口,无需 GPU 与数据。
