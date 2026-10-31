# 2D/3D Grounding 实践与坐标格式体系

## 1. 本讲目标

学完本讲，你应该能够：

1. 准确区分 Qwen-VL 三代模型的坐标口径：Qwen2-VL 的 0–1000 相对坐标、Qwen2.5-VL 的「缩放后图像上的绝对像素坐标」、Qwen3-VL 默认回归的 0–1000 相对坐标。
2. 解析模型输出的四种定位格式：`bbox_2d` JSON、`point_2d` JSON、XML `<points>` 格式、`bbox_3d` 九元组。
3. 用「反算 + 画框」的方式可视化验证任何 grounding 输出或训练标注——这是发现坐标口径错位的最有效手段。
4. 把一份外部检测标注（如 COCO 格式）转换成 qwen-vl-finetune 训练可用的 `value` 文本。

本讲是单元七的第三讲，承接 u7-l1 的「坐标统一 0～1000 相对制」结论与 u7-l2 的「虚拟分辨率 999×1000」技巧，把它们落实到源码层面；同时向后衔接 u7-l4 的自定义数据微调闭环。

## 2. 前置知识

**Grounding（视觉定位）**：让模型根据文字描述在图中指出目标位置的任务统称。输出可以是矩形框（bbox，适合检测）、单点（point，适合指代）、3D 长方体（bbox_3d，适合自动驾驶/机器人场景）。

**绝对坐标与相对坐标**：
- 绝对像素坐标：`[146, 137, 615, 415]` 直接表示「图中第 146 列、第 137 行的像素点」。它绑定某一张具体尺寸的图。
- 相对归一化坐标：把宽和高各自除以图的真实宽高，再乘一个基准（Qwen 系用 1000），得到与分辨率无关的数值。换算关系为：

\[
x_{\text{abs}} = \frac{x_{\text{rel}}}{1000} \times W, \qquad y_{\text{abs}} = \frac{y_{\text{rel}}}{1000} \times H
\]

**bbox 的角点顺序**：Qwen-VL 全系约定 `bbox_2d = [x1, y1, x2, y2]`，即左上角 `(x1, y1)`、右下角 `(x2, y2)`。注意 COCO 标注用的是 `[x, y, w, h]`（左上角 + 宽高），转换时必须先换算成角点（见 4.4）。

**IoU（交并比）**：两个框的交集面积除以并集面积，是衡量定位准确度的标准指标（u6-l4 的 ODinW-13 评测已用过）：

\[
\mathrm{IoU} = \frac{|A \cap B|}{|A \cup B|}
\]

**Markdown fencing（围栏）**：模型经常把 JSON 包在 ```` ```json ... ``` ```` 代码块里输出，解析前要先剥掉这层壳。

**针孔相机模型（3D 部分用）**：3D 坐标 `(X, Y, Z)`（米，相机坐标系）投影到像素平面只需 4 个内参——焦距 `fx, fy` 与主点 `cx, cy`：

\[
x_{2d} = f_x \cdot \frac{X}{Z} + c_x, \qquad y_{2d} = f_y \cdot \frac{Y}{Z} + c_y
\]

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| `cookbooks/2d_grounding.ipynb` | 推理侧 2D 定位 cookbook：坐标口径变更说明、bbox/point 解析与可视化工具、五类定位提示模板、已知限制 |
| `cookbooks/3d_grounding.ipynb` | 推理侧 3D 定位 cookbook：`bbox_3d` 九元组格式、JSON 解析、3D 框投影画图、相机内参的加载与估算 |
| `qwen-vl-finetune/tools/process_bbox.ipynb` | 训练侧坐标转换工具：把外部绝对坐标分别转成 Qwen2-VL（相对）与 Qwen2.5-VL（缩放后绝对）两种训练口径 |
| `qwen-vl-finetune/README.md`（辅助） | 微调数据格式中的 Grounding 示例（u4-l2 已讲格式，本讲聚焦其坐标数值） |
| `evaluation/ODinW-13/run_odinw.py`（辅助） | 评测侧同时支持两种口径的反算分支 `is_rel`，是「两种口径并存」的仓库级证据（u6-l4 已讲结构） |

> 提示：notebook 的永久链接指向 `.ipynb` 原始 JSON 的行号（GitHub 对 ipynb 的锚点方式）。每个引用都给出该行所在的单元格功能说明。

## 4. 核心概念与源码讲解

### 4.1 相对坐标约定

#### 4.1.1 概念说明

Qwen-VL 三代模型用了三种不同的坐标口径，这是 grounding 任务里最容易踩坑的地方——模型输出的数字本身永远长得很像（一串 0–1000 或几百的整数），但「除以谁」完全不同：

| 模型代 | 坐标口径 | 数值范围 | 反算为像素的方法 |
| --- | --- | --- | --- |
| Qwen2-VL | 相对坐标（宽高各自归一化） | 0–1000 | `abs = rel / 1000 × W(或 H)` |
| Qwen2.5-VL | 缩放后图像上的绝对坐标 | 0–resized_H/W | `abs = coord / resized_W × W`（需先算出 resized 尺寸） |
| Qwen3-VL | **默认相对坐标**（回归 Qwen2-VL 风格） | 0–1000 | `abs = rel / 1000 × W(或 H)` |

官方 notebook 对 Qwen3-VL 的变更写得很直白：默认坐标系从 Qwen2.5-VL 的绝对坐标改回 0–1000 相对坐标，并且括号里强调「你不再需要计算 resized_w」。这句话的含义是：Qwen2.5-VL 口径下，消费方必须先复现 `smart_resize`（u2-l2 精读过）才能知道坐标分母；Qwen3-VL 相对口径下，分母恒为 1000，与图像怎么缩放完全解耦。

这也解释了两个此前讲过的现象：

- u7-l2 的 GUI Agent 虚拟分辨率技巧：把屏幕声明为 999×1000，模型输出的「像素坐标」事实上就是 0–1000 相对坐标——同一个约定的 Agent 侧应用。
- u6-l4 的 ODinW-13 评测保留 `is_rel` 环境变量双分支：因为被测模型可能是 2.5（绝对）或 3 代（相对）口径，判分时必须选对分母。

#### 4.1.2 核心流程

坐标在整条链路上的流转可以画成一条「正反算」通路：

```text
【训练侧】原始标注(绝对像素, 如 COCO)
      │  按目标口径正算: rel = abs / W × 1000  (或 2.5 口径: abs × resized_W / W)
      ▼
训练样本 gpt value 文本: {"bbox_2d": [...], "label": "..."}
      │  (微调后模型学会该口径)
      ▼
【推理侧】模型输出同口径数字
      │  反算回像素: abs = rel / 1000 × W  (或 / resized_W × W)
      ▼
可视化 / IoU 判分
```

核心原则只有一句话：**同一条数据的「标注口径、提示语约定、反算分母」三处必须一致**，任何一处错位，框就会画飞，但程序不会报任何错。

#### 4.1.3 源码精读

- 官方口径声明：[cookbooks/2d_grounding.ipynb:L13](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/2d_grounding.ipynb#L13)（首个 markdown 单元格）明确写出 Qwen3-VL 默认坐标系改为 0–1000 相对坐标、无需计算 resized_w，并同时声明改进了多目标 grounding 能力。这是全仓库对 Qwen3-VL 坐标口径最权威的一句说明。

- 相对坐标反算的实现：[cookbooks/2d_grounding.ipynb:L189-L192](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/2d_grounding.ipynb#L189-L192)（`plot_bounding_boxes` 内部）把 `bbox_2d` 的四个分量逐一执行 `int(坐标 / 1000 × width或height)`——分母恒为 1000，宽用 `width`、高用 `height`，这正是相对口径的标准反算。

- 评测侧双口径分支：[evaluation/ODinW-13/run_odinw.py:L239-L245](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/ODinW-13/run_odinw.py#L239-L245) 中，环境变量 `is_rel=1` 时按 `÷1000 × 原图宽高` 反算（Qwen3-VL 相对口径），否则按 `÷resized宽高 × 原图宽高` 反算（Qwen2.5-VL 绝对口径）。同一份评测代码里并存的两个分母，就是三代口径差异最直观的仓库级证据。

- 定位任务的像素预算：[cookbooks/2d_grounding.ipynb:L376](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/2d_grounding.ipynb#L376)（`inference_with_openai_api` 签名）默认 `min_pixels=64*32*32, max_pixels=9800*32*32`。按 u2-l2 的换算尺（1 视觉 token = 32×32 像素），这是「单图 64～9800 个视觉 token」——grounding 要看清小物体，上限开到了接近 1 万 token 的高清档，与 u7-l1「按任务定像素档」的结论一致。

#### 4.1.4 代码实践

1. **实践目标**：用具体数字验证「相对坐标与分辨率无关」，并体会绝对坐标为什么必须知道 resized 尺寸。
2. **操作步骤**（示例代码，本地 Python 即可运行，无需模型）：

   ```python
   # 示例代码：验证相对坐标的分辨率无关性
   bbox_rel = [228, 320, 961, 970]   # 0-1000 相对坐标
   for W, H in [(640, 428), (1280, 856), (320, 214)]:
       x1 = int(bbox_rel[0] / 1000 * W)
       y1 = int(bbox_rel[1] / 1000 * H)
       print(f"{W}x{H} -> 左上角像素 ({x1}, {y1})")
   ```

3. **需要观察的现象**：同一组相对坐标在三种分辨率下反算出的像素点都落在画面中同一个「相对位置」上（左上角比例恒为 0.228、0.320）。
4. **预期结果**：输出三行像素坐标，数值随分辨率等比缩放。若换成 Qwen2.5-VL 的绝对口径坐标，不做 `÷resized_W` 换算直接用到另一种尺寸的图上，位置就会错。
5. 结论可用手算验证：`228/1000×640 = 146`，`228/1000×1280 = 292`，比例不变。

#### 4.1.5 小练习与答案

**练习 1**：某 Qwen3-VL 输出 `bbox_2d = [500, 500, 600, 600]`，画布宽 1920、高 1080。反算出的像素框左上角是多少？为什么 x、y 不能用同一个分母？

**答案**：`x1 = 500/1000×1920 = 960`，`y1 = 500/1000×1080 = 540`。因为相对坐标是「宽归一化除以宽、高归一化除以高」，两个方向的基准长度不同，必须分别乘 `W` 和 `H`。

**练习 2**：为什么 Qwen2.5-VL 口径下「You don't need to calculate the resized_w」这句话不成立？

**答案**：Qwen2.5-VL 输出的是缩放后图像上的绝对坐标，反算必须先知道 `smart_resize` 之后的宽高（即 resized_w/h）作分母；而 resized 尺寸取决于 `min_pixels/max_pixels` 预算参数（u2-l2），消费方必须复现同一套缩放逻辑，否则分母就错了。Qwen3-VL 相对口径的分母恒为 1000，因此不再需要。

### 4.2 bbox 与 point 输出解析

#### 4.2.1 概念说明

Qwen3-VL 的定位输出有四种格式，前三种是 2D，最后一种是 3D：

| 格式 | 输出形态 | 典型提示语 | 关键字段 |
| --- | --- | --- | --- |
| 框（JSON） | `[{"bbox_2d": [x1,y1,x2,y2], "label": "..."}]` | 「Report bbox coordinates in JSON format」 | `bbox_2d`、`label` |
| 点（JSON） | `[{"point_2d": [x,y], "label": "..."}]` | 「Locate ... with points」 | `point_2d`、`label` |
| 点（XML） | `<points x1="707" y1="233" alt="...">...</points>` | 「output its coordinates in XML format」 | `x1/y1` 属性 |
| 3D 框（JSON） | `[{"bbox_3d": [9 个数], "label": "..."}]` | 「provide its 3D bounding box」 | `bbox_3d` 九元组 |

与 u7-l1 总结的「JSON schema 内嵌于提示、键名即下游解析协议」一脉相承：提示语里写什么样的示例 schema，模型就照着什么键名输出。你还可以在 schema 里追加自定义键（如车辆颜色、球员角色），模型会一并填充——这是官方 Example 4/5 演示的「带属性的多目标定位」。

`bbox_3d` 九元组的语义是 `[x_center, y_center, z_center, x_size, y_size, z_size, roll, pitch, yaw]`：前三个是物体中心在相机坐标系中的位置（米），中间三个是长宽高（米），后三个是旋转角。注意一个细节：模型输出的角度是「乘过 180 的缩放值」，可视化时还要再乘 180 还原（见 4.2.3）。

#### 4.2.2 核心流程

解析一段模型回复的通用流水线：

```text
模型原始回复
  │ ① 剥 markdown 围栏（```json ... ```）
  │ ② 定位 JSON 主体（首个 '[' 到最后一个 ']'，或 json 围栏之间的内容）
  │ ③ 文本 → 对象：ast.literal_eval 或 json.loads（失败则截断到最后一个 '"}' 再试）
  │ ④ 归一化成 list（单个 dict 也包成 list）
  ▼
[{...}, {...}]  可供反算/画框/判分
```

对 XML 点格式，上述 JSON 流水线不适用，需按属性解析（`x1`/`y1`）。

#### 4.2.3 源码精读

- 剥围栏：[cookbooks/2d_grounding.ipynb:L270-L281](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/2d_grounding.ipynb#L270-L281)（`parse_json`）逐行扫描，找到 ```` ```json ```` 行后取其后全部内容、再截到闭合 ```` ``` ```` 为止。这是 2D notebook 全部 bbox 解析的预处理步骤。

- 文本转对象与截断容错：[cookbooks/2d_grounding.ipynb:L174-L178](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/2d_grounding.ipynb#L174-L178)（`plot_bounding_boxes` 内部）先 `ast.literal_eval`，失败则定位最后一个 `"}"`、截断后补 `]` 再解析——专门兜底「多目标太多导致回复被截断」的情况（与 4.3.3 的已知限制相呼应）。

- 点 JSON 解析：[cookbooks/2d_grounding.ipynb:L96-L122](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/2d_grounding.ipynb#L96-L122)（`decode_json_points`）剥围栏后 `json.loads`，逐项提取 `point_2d` 的 `(x, y)` 与可选 `label`（缺省自动命名为 `point_N`）。配套的 `plot_points_json` 在 [L242-L267](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/2d_grounding.ipynb#L242-L267) 用 `point_2d/1000×宽高` 反算（[L259](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/2d_grounding.ipynb#L259)）。

- XML 点格式：提示模板见 [cookbooks/2d_grounding.ipynb:L1164](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/2d_grounding.ipynb#L1164)（`<points x y>object</points>`），真实回复保存在 [L1143](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/2d_grounding.ipynb#L1143) 的单元格输出中：`<points x1="707" y1="233" alt="rolling pin ...">rolling pin ...</points>`。批判性阅读：该示例单元格调用的是 `plot_points`→`decode_json_points`，而后者只会 `json.loads`，对 XML 文本会解析失败并返回空列表——即官方 notebook 对 XML 格式并未提供配套可视化，需要自己写属性解析（练习 3 会让你补上）。

- 3D 解析：[cookbooks/3d_grounding.ipynb:L68-L107](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/3d_grounding.ipynb#L68-L107)（`parse_bbox_3d_from_text`）优先取 ```` ```json ```` 围栏内文本，否则取首个 `[` 到最后一个 `]`，解析后把单个 dict 归一化为 list。

- 3D 九元组格式定义：[cookbooks/3d_grounding.ipynb:L213](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/3d_grounding.ipynb#L213)（markdown 单元格）给出 `[x_center, y_center, z_center, x_size, y_size, z_size, roll, pitch, yaw]` 的逐项语义（相机坐标、米、弧度制说明）。

- 角度缩放 quirk：[cookbooks/3d_grounding.ipynb:L176](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/3d_grounding.ipynb#L176)（`draw_3dbboxes` 内部）在投影前对九元组最后三个角度执行 `_x * 180`——即模型输出的角度是「真实角度 × π/180 再乘 180」的缩放表示，消费时必须还原，否则 3D 框会乱转。

- 提示模板三例：类别清单多目标检测 [cookbooks/2d_grounding.ipynb:L484](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/2d_grounding.ipynb#L484)（`locate every instance that belongs to the following categories: "..."`，官方注明支持负样本类别、生成时会跳过）；带自定义键的框定位 [L871](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/2d_grounding.ipynb#L871)（schema 中追加 `type`、`color`）；带属性的点定位 [L946](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/2d_grounding.ipynb#L946)（追加 `role`、`shirt_color`）；3D 检测 [cookbooks/3d_grounding.ipynb:L342](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/3d_grounding.ipynb#L342)。

#### 4.2.4 代码实践

1. **实践目标**：写一个统一入口 `parse_grounding(text)`，同时吃下 JSON 框、JSON 点、XML 点三种回复。
2. **操作步骤**（示例代码）：

   ```python
   # 示例代码：统一解析三种 2D 定位输出
   import ast, json, re

   def strip_fence(text):
       if "```json" in text:                      # 与 parse_json 同思路
           return text.split("```json")[1].split("```")[0]
       return text

   def parse_grounding(text):
       t = strip_fence(text)
       if t.lstrip().startswith("<points"):       # XML 点格式
           return [{"point_2d": [int(m.group(1)), int(m.group(2))],
                    "label": m.group(3)}
                   for m in re.finditer(r'<points x1="(\d+)" y1="(\d+)"[^>]*>([^<]+)</points>', t)]
       try:
           data = ast.literal_eval(t.strip())     # 与 plot_bounding_boxes 同思路
       except (ValueError, SyntaxError):
           data = json.loads(t.strip())
       return data if isinstance(data, list) else [data]

   print(parse_grounding('<points x1="707" y1="233" alt="pin">rolling pin</points>'))
   print(parse_grounding('```json\n[{"bbox_2d": [74, 361, 324, 808], "label": "plate/dish"}]\n```'))
   ```

3. **需要观察的现象**：两行输出分别是一个 `point_2d` 字典和一个 `bbox_2d` 字典列表；XML 分支抽出的坐标是 `(707, 233)`，与官方 notebook 保存的真实回复一致。
4. **预期结果**：`[{'point_2d': [707, 233], 'label': 'rolling pin'}]` 与 `[{'bbox_2d': [74, 361, 324, 808], 'label': 'plate/dish'}]`。纯文本处理，本地可直接运行验证。

#### 4.2.5 小练习与答案

**练习 1**：提示语里没写输出 schema，模型会按什么键名输出 bbox？

**答案**：按模型训练时内置的默认协议，即 `bbox_2d`（框）/`point_2d`（点）加 `label` 的 JSON 格式；官方 notebook 在「默认系统提示」一节也注明 bbox 与 point 的默认格式均为 JSON。只有想要自定义键（如 `color`、`role`）时才需要在提示里显式给出 schema 示例。

**练习 2**：`parse_bbox_3d_from_text` 为什么在找不到 ```` ```json ```` 围栏时改用「首个 `[` 到最后一个 `]`」定位？

**答案**：模型有时会把 JSON 直接嵌在自然语言里输出（前后带说明文字），围栏可能不存在；取首个 `[` 与最后一个 `]` 能最大限度包住完整的 JSON 数组。若两者都找不到则返回空列表，交给上层跳过该样本。

**练习 3**：为 XML 点格式写一个正则解析器，输出与 `decode_json_points` 相同的 `(points, labels)` 结构。

**答案**：核心正则 `r'<points x1="(\d+)" y1="(\d+)"[^>]*>([^<]*)</points>'`，对每个匹配收集 `[int(x1), int(y1)]` 与标签文本即可（4.2.4 示例代码已实现）。注意坐标同样要按 0–1000 相对口径反算才能画图。

### 4.3 可视化验证

#### 4.3.1 模块说明（概念）

坐标口径错位不会让程序报错，只会让框画错位置——所以 grounding 开发的第一守则是**任何输出、任何标注，先画到图上看一眼**。可视化之所以能当「检验器」，是因为反算公式里分母写错了（1000 vs resized_w）、x/y 弄反了、角点顺序弄反了，画出来分别是完全不同且肉眼可辨的错误模式（整体漂移、沿对角线镜像、框变成一条线等）。

2D notebook 的做法是把「解析 → 反算 → 画框写标签」封装成 `plot_bounding_boxes` / `plot_points_json`；3D notebook 则更进一步：把 3D 框的 8 个角点用相机内参投影回 2D，再连 12 条棱。

#### 4.3.2 核心流程

`plot_bounding_boxes` 的执行过程：

```text
传入 PIL 图像 + 模型回复文本
  ① parse_json 剥围栏
  ② ast.literal_eval 转对象（失败→截断到最后一个 '"}' 补 ']' 重试）
  ③ 逐框：bbox_2d 四分量 ÷1000 × (width 或 height) 反算像素
  ④ 若 x1>x2 / y1>y2 则交换（容忍模型输出反序角点）
  ⑤ draw.rectangle 画框；有 label 则在框内左上角写文字
```

3D 可视化 `draw_3dbboxes` 的执行过程：

```text
  ① parse_bbox_3d_from_text 得到若干 bbox_3d
  ② 九元组末三位角度 ×180 还原
  ③ 由中心+尺寸生成 8 个局部角点 → 按 roll/pitch/yaw 旋转 → 平移到相机坐标
  ④ 针孔投影 x2d = fx·X/Z + cx（Z>0 才投影）
  ⑤ 连 12 条棱画出 3D 长方体
```

#### 4.3.3 源码精读

- 反算与画框主体：[cookbooks/2d_grounding.ipynb:L124-L210](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/2d_grounding.ipynb#L124-L210)（`plot_bounding_boxes`）。其中 [L189-L192](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/2d_grounding.ipynb#L189-L192) 执行 ÷1000 反算；[L194-L195](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/2d_grounding.ipynb#L194-L195) 交换反序的 x1/x2；随后 `draw.rectangle` 画框、`draw.text` 写 `label`。批判性阅读：函数 docstring 写的是「normalized [y1 x1 y2 x2] format」，但代码实际按下标 `[0]=x1, [1]=y1, [2]=x2, [3]=y2` 取值——以代码为准，docstring 是历史遗留笔误。

- 分辨率无关的直接证据：示例单元格在画框前执行 `image.thumbnail([640,640])`（如 [cookbooks/2d_grounding.ipynb:L484-L497](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/2d_grounding.ipynb#L484-L497) 的 Example 1：取图 → thumbnail → plot）。thumbnail 把显示尺寸缩到 640 以内，框依然画得准——正因为相对坐标的分母是 1000 而不是某个固定分辨率。官方保存的 Example 1 真实输出（[L436-L462](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/2d_grounding.ipynb#L436-L462)）是 20 多个 `{"bbox_2d": [74, 361, 324, 808], "label": "plate/dish"}` 这样的条目，数值全部落在 0–999 之间，可作口径的实例印证。

- 3D 投影：[cookbooks/3d_grounding.ipynb:L110-L150](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/3d_grounding.ipynb#L110-L150)（`convert_3dbbox`）。[L114](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/3d_grounding.ipynb#L114) 起由半尺寸生成 8 个局部角点；[L142-L148](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/3d_grounding.ipynb#L142-L148) 对每个角点旋转、平移后，在 `Z > 0`（位于相机前方）时按针孔模型投影出像素坐标。

- 画 3D 框与角度还原：[cookbooks/3d_grounding.ipynb:L152-L207](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/3d_grounding.ipynb#L152-L207)（`draw_3dbboxes`）按 12 条棱连线；[L176](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/3d_grounding.ipynb#L176) 处先对末三位角度乘 180（见 4.2.3 的 quirk）。

- 相机内参的两种来源：[cookbooks/3d_grounding.ipynb:L284-L288](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/3d_grounding.ipynb#L284-L288)（`load_camera_params`）从仓库自带的 `cookbooks/assets/spatial_understanding/cam_infos.json` 读取每张示例图的真实内参（如 `autonomous_driving.jpg` 的 `fx≈1266.4`）；[L290-L315](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/3d_grounding.ipynb#L290-L315)（`generate_camera_params`）在没有内参时按 \( f_x = \frac{W}{2\tan(\mathrm{fov}/2)} \)、主点取图像中心，用 fov=60° 估算一组伪内参——官方也明说 3D 感知高度依赖相机参数，这组伪参数仅供演示。

- 已知限制：[cookbooks/2d_grounding.ipynb:L1364-L1366](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/2d_grounding.ipynb#L1364-L1366)（Known Limitations）写明两条：单类别实例数显著超过 40–50 或物体过密时可能陷入无限生成（这正是 4.2.3 截断容错存在的原因）；相邻密集物体偶尔会被合并成一个框，可用更精细的提示缓解。

#### 4.3.4 代码实践

1. **实践目标**：对一张含多个物体的图片完成「定位 → 解析 → 画框可视化」闭环（本讲义任务的前半）。
2. **操作步骤**：
   - 取图：可直接用仓库自带样例 `cookbooks/assets/spatial_understanding/dining_table.png`（或自己的多物体照片）。
   - 调模型（二选一）：
     - 路线 A（官方 cookbook 路线）：参考 [cookbooks/2d_grounding.ipynb:L376-L413](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/2d_grounding.ipynb#L376-L413) 的 `inference_with_openai_api`，配置 DashScope API Key，模型用 `qwen3-vl-235b-a22b-instruct`。
     - 路线 B（本地路线，接 u1-l3）：transformers 加载 `Qwen/Qwen3-VL-2B-Instruct`，messages 中放 `{"type": "image", "image": "路径"}` 加文本元素，提示语照抄官方模板。
   - 提示语照抄 [L484](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/2d_grounding.ipynb#L484)：`locate every instance that belongs to the following categories: "cup, bowl, spoon". Report bbox coordinates in JSON format.`
   - 解析画框（示例代码）：

   ```python
   # 示例代码：解析 + 反算 + 画框
   from PIL import Image, ImageDraw

   def draw_boxes(im, items):
       draw, (W, H) = ImageDraw.Draw(im), im.size
       for it in items:
           x1, y1, x2, y2 = it["bbox_2d"]
           box = [int(x1/1000*W), int(y1/1000*H), int(x2/1000*W), int(y2/1000*H)]
           draw.rectangle(box, outline="red", width=3)
           draw.text((box[0]+8, box[1]+6), it.get("label", ""), fill="red")
       im.show()

   # im = Image.open("cookbooks/assets/spatial_understanding/dining_table.png").convert("RGB")
   # im.thumbnail([640, 640])
   # draw_boxes(im, parse_grounding(model_response))
   ```

3. **需要观察的现象**：解析出的 `bbox_2d` 数值全部落在 0–1000 区间；画出的框分别套在对应类别物体上，标签文字在框内左上角。
4. **预期结果**：多目标各自成框、类别正确。模型生成部分依赖 API 或本地 GPU，具体输出**待本地验证**；解析与画框部分可用官方保存的真实回复（[L436-L462](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/2d_grounding.ipynb#L436-L462)）离线先行验证。

#### 4.3.5 小练习与答案

**练习 1**：把反算分母从 1000 改成 512 再画框，框会怎么错？这能用来排查什么问题？

**答案**：框整体偏小并向左上收缩（512 < 1000 时坐标被缩小到约一半位置）。这类「整体等比漂移」是分母错误的典型指纹；反之若框沿对角线镜像，通常是 x/y 弄反或角点顺序弄反。先画框再排查，比逐行读代码快得多。

**练习 2**：为什么官方在画框前 `thumbnail([640,640])` 而不需要改坐标？如果模型输出的是 Qwen2.5-VL 绝对口径，这一步还能这样写吗？

**答案**：相对坐标反算用的是「当前图像自身的 width/height」，thumbnail 后的尺寸就是反算基准，框自然落在正确位置。Qwen2.5-VL 绝对口径绑定 resized 图尺寸，缩放显示前必须先 `÷resized_W×当前W` 换算（正如 ODinW-13 的 `is_rel=0` 分支），不能直接画。

**练习 3**：3D 可视化中为什么要有 `Z > 0` 判断？

**答案**：针孔模型只对位于相机前方（光轴正方向）的点有意义；`Z ≤ 0` 的点投影公式会除以负数或零，产生无意义甚至溢出的像素坐标，跳过它们才能保证画出的 3D 框棱边合理。

### 4.4 训练数据 bbox 转换

#### 4.4.1 模块说明（概念）

微调一个会 grounding 的模型（或让模型保持你要的坐标口径），训练数据里的 gpt 回答必须是目标口径的坐标文本。而外部检测数据集（COCO、LVIS 等）的标注几乎都是**原图上的绝对像素坐标**，且常用 `[x, y, w, h]` 格式——转换必不可少。

`qwen-vl-finetune/tools/process_bbox.ipynb` 就是官方提供的转换参考，README 的 tools 一节明确其用途：「Convert bbox into QwenVL format. If you have grounding data, please refer this file to transform your data」。它实现了两条转换路径：

- `convert_to_qwen2vl_format`：转 0–1000 相对坐标（Qwen2-VL 口径）；
- `convert_to_qwen25vl_format`：先复现 `smart_resize`（factor=28），再按缩放比转成 resized 图上的绝对坐标（Qwen2.5-VL 口径）。

对 Qwen3-VL：推理默认口径是相对 0–1000（4.1），因此**若希望微调后的模型保持官方默认输出习惯，制作数据时应采用「÷宽高×1000」的相对口径**，即 `convert_to_qwen2vl_format` 的公式（函数名虽叫 qwen2vl，公式本身就是相对口径）。仓库没有在微调侧强制坐标系——标签只是文本，口径由数据制作者决定，但「训练口径」必须与「你日后解析输出的反算分母」一致。

#### 4.4.2 核心流程

一条外部标注变成训练样本的完整流水线：

```text
COCO 标注 {"bbox": [x, y, w, h], "category": ...}   （原图绝对像素）
  ① xywh → xyxy：x1=x, y1=y, x2=x+w, y2=y+h
  ② 选定目标口径并换算：
       相对口径:  v = round(coord / 原宽或高 × 1000)，再夹紧到 [0,1000]
       2.5 口径:  先 smart_resize(原H,原W) 得 (newH,newW)，
                  v = round(coord × new_W / 原W)，夹紧到 [0, new_W-1]
  ③ 拼成 JSON 字符串: {"bbox_2d": [x1,y1,x2,y2], "label": "类别名"}
  ④ 嵌入训练样本:
       {"image": "路径", "conversations": [
           {"from": "human", "value": "<image>\nLocate ... in JSON format."},
           {"from": "gpt",   "value": "{\"bbox_2d\": [...], \"label\": \"...\"}"}]}
```

第 ④ 步的样本结构、`<image>` 占位符配对规则与注册流程已在 u4-l2/u4-l3 讲过，本讲只聚焦坐标数值这一层。

#### 4.4.3 源码精读

- 原始标注与可视化：[qwen-vl-finetune/tools/process_bbox.ipynb:L38-L55](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/tools/process_bbox.ipynb#L38-L55) 第一个单元格定义示例标注 `{"bbox_2d": [146, 137, 615, 415], "label": "bowl of carrots"}`，用 OpenCV 读入 `../demo/images/COCO_train2014_000000580957.jpg` 画框。单元格保存的输出显示该图 `height and width is: (428, 640)`——高 428、宽 640，这两个数是后面全部换算的分母。

- 相对口径转换：[qwen-vl-finetune/tools/process_bbox.ipynb:L87-L103](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/tools/process_bbox.ipynb#L87-L103)（`convert_to_qwen2vl_format`）对四个分量执行 `round(coord / w或h × 1000)` 并用 `max(0, min(v, 1000))` 夹紧。单元格保存的输出为 `[228, 320, 961, 970]`，手算可核对：`146/640×1000=228.1→228`、`137/428×1000=320.1→320`、`615/640×1000=960.9→961`、`415/428×1000=969.6→970`。

- 2.5 口径的 resize 复现：[qwen-vl-finetune/tools/process_bbox.ipynb:L130-L155](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/tools/process_bbox.ipynb#L130-L155) 原样复制了 Qwen2.5-VL 的 `smart_resize`（`factor=28`，即 u2-l2 精读过的对齐取整 + 像素预算算法；注意这里 factor 是 28，不是 Qwen3-VL 的 32）。对 428×640 的图：`round(428/28)×28=420`、`round(640/28)×28=644`，预算区间内不再调整，得 resized 尺寸 `(420, 644)`。

- 2.5 口径转换：[qwen-vl-finetune/tools/process_bbox.ipynb:L157-L176](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/tools/process_bbox.ipynb#L157-L176)（`convert_to_qwen25vl_format`）先 `smart_resize` 得新尺寸，按 `scale_w=644/640、scale_h=420/428` 缩放坐标，再夹紧到 `[0, new_w-1]/[0, new_h-1]`。保存的输出为 `[147, 134, 619, 407]`，手算核对：`146×1.00625=146.9→147`、`137×0.9813=134.4→134`、`615×1.00625=618.8→619`、`415×0.9813=407.2→407`。注意与相对口径的夹紧差异：绝对口径上界是 `new_w-1` 而非 1000。

- 训练侧 grounding 样本长什么样：[qwen-vl-finetune/README.md:L106-L118](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/README.md#L106-L118)（Grounding Example）human 问句 `Locate house in this image and output the bbox coordinates in JSON format.`，gpt 回答是 `{"bbox_2d": [135, 114, 1016, 672]}` 形式的 JSON 文本。批判性阅读：该示例引用的图正是上面那张 428×640 的 COCO 图，而 `x2=1016` 既超出 0–1000 相对口径的上界，也超出原图宽 640——这组数值更像格式示意而非真实标注。它恰好演示了「不画框验证就会把错口径数据喂进训练」的风险；制作自己的数据时，请以 4.3 的可视化流程对每批标注抽样画框。

- 工具定位说明：[qwen-vl-finetune/README.md:L23](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/README.md#L23) 把 `process_bbox.ipynb` 列为 tools 三件套之一，与 `pack_data.py`（u4-l4）并列，均在训练前对数据做预处理。

#### 4.4.4 代码实践

1. **实践目标**：把一份 COCO 格式标注转成 Qwen3-VL 相对口径的训练 `value` 文本（本讲义任务的后半）。
2. **操作步骤**（示例代码，可直接本地运行）：

   ```python
   # 示例代码：COCO → Qwen3-VL 相对口径训练样本
   from PIL import Image

   def coco_to_sample(image_path, coco_anns):
       W, H = Image.open(image_path).size          # 原图宽高作分母
       convs = []
       for ann in coco_anns:                        # COCO: bbox=[x,y,w,h] 绝对像素
           x, y, w, h = ann["bbox"]
           label = ann["label"]
           x1 = max(0, min(round(x / W * 1000), 1000))
           y1 = max(0, min(round(y / H * 1000), 1000))
           x2 = max(0, min(round((x + w) / W * 1000), 1000))
           y2 = max(0, min(round((y + h) / H * 1000), 1000))
           convs.append({
               "image": image_path,
               "conversations": [
                   {"from": "human",
                    "value": "<image>\nLocate " + label + " in this image and output the bbox coordinates in JSON format."},
                   {"from": "gpt",
                    "value": '{"bbox_2d": [%d, %d, %d, %d], "label": "%s"}' % (x1, y1, x2, y2, label)},
               ],
           })
       return convs

   anns = [{"bbox": [146, 137, 469, 278], "label": "bowl of carrots"}]  # 469=615-146, 278=415-137
   print(coco_to_sample("demo/images/COCO_train2014_000000580957.jpg", anns))
   ```

3. **需要观察的现象**：gpt `value` 中的坐标应为 `[228, 320, 961, 970]`，与 `convert_to_qwen2vl_format` 的官方输出逐位一致；human `value` 以 `<image>` 占位符开头（u4-l2 的配对规则）。
4. **预期结果**：打印出一条完整训练样本，坐标四位数字均落在 0–1000。随后可复用 4.3 的画框代码把 `[228,320,961,970]` 反算画回原图，确认框仍套在胡萝碗上——正反算闭环即验证通过。此步纯本地可验证。
5. 之后再按 u4-l3 把生成的 JSON 注册进 `data_dict`、按 u7-l4 的流程做 LoRA 微调，即完成从标注到模型的闭环。

#### 4.4.5 小练习与答案

**练习 1**：同一坐标 `[146, 137, 615, 415]` 分别转 Qwen2-VL 与 Qwen2.5-VL 口径，结果为何一个是 `[228, 320, 961, 970]`、一个是 `[147, 134, 619, 407]`？哪个「数字更接近原值」？

**答案**：前者除以原图宽高再乘 1000（进入 0–1000 量纲），后者按 `smart_resize` 的缩放比近似原样平移（428×640 → 420×644，缩放比≈1，所以数值只微调到 147/134/619/407）。2.5 口径的数字更接近原值，因为它仍是绝对像素坐标，只是换到了 resized 图上；但正因为它依赖 resized 尺寸，消费端必须知道预算参数才能反算。

**练习 2**：为什么两种转换的夹紧上界不同（1000 vs `new_w-1`）？

**答案**：相对口径的合法区间是 [0,1000]（1000 表示边界）；绝对口径的合法区间是 resized 图的实际像素范围，x 方向最大有效下标是 `new_w-1`。夹紧是为了兜住标注越界（如框略超出图像边缘）导致的溢出值。

**练习 3**：如果你要为 Qwen3-VL 准备 grounding 数据，`convert_to_qwen25vl_format` 里复现的 `smart_resize` 用 `factor=28`，直接照抄会有什么隐患？

**答案**：factor=28 是 Qwen2/2.5-VL 的 patch 口径（2×14），Qwen3-VL 的 patch 边长是 16、对齐因子是 32（u2-l1/u2-l2）。若走相对口径则根本不需要 smart_resize（分母就是原图宽高与 1000），`factor` 不参与计算；只有当你坚持用绝对口径标注时，才必须把 factor 换成与训练侧 processor 一致的 32，否则 resized 尺寸与训练时不一致，坐标语义错位。这也再次说明相对口径对 Qwen3-VL 是更省心的选择。

## 5. 综合实践

**任务：做一个「定位—验证—再造数据」的小工具链**，把本讲四个模块串起来：

1. **推理**：挑一张含 3 类以上物体的图（可直接用 `cookbooks/assets/spatial_understanding/lots_of_people.jpeg`），用 4.1.3 的官方提示模板（类别清单式）做一次多目标定位，走 API 路线（`inference_with_openai_api`）或本地 2B 模型路线均可。
2. **解析**：用 4.2.4 的 `parse_grounding` 解析回复，打印条目数与各条 `label`。
3. **验证**：用 4.3.4 的 `draw_boxes` 把框画回原图并保存为 PNG；故意把分母改成 512 再画一张，对比两图差异并截图记录。
4. **再造数据**：手写（或从任意 COCO 标注文件读取）同一张图的 3 条 `[x,y,w,h]` 绝对坐标标注，用 4.4.4 的 `coco_to_sample` 生成相对口径训练样本 JSON；用第 3 步的画框函数反算验证生成的 `bbox_2d` 落点正确。
5. **（可选进阶）**：把样本 JSON 按 u4-l3 注册进 `qwen-vl-finetune/qwenvl/data/__init__.py` 的 `data_dict`，再按 u7-l4 流程跑一次 LoRA 冒烟微调，观察微调后模型输出的坐标口径是否与训练标注一致。

产出物：一张正确画框图、一张错误分母对照图、一份 `my_grounding.json`。涉及模型推理的部分**待本地验证**；解析、转换、画框部分可离线完成。

## 6. 本讲小结

- Qwen-VL 三代三种坐标口径：Qwen2-VL 相对 0–1000、Qwen2.5-VL resized 绝对像素、Qwen3-VL 默认回归相对 0–1000；反算分母分别是 1000 与 resized 宽高，「口径—提示—分母」三处必须一致。
- 输出格式四种：`bbox_2d` JSON、`point_2d` JSON、XML `<points>`、`bbox_3d` 九元组（中心/尺寸/姿态，角度消费前需 ×180 还原）；schema 内嵌于提示语，可追加自定义属性键。
- 解析流水线固定为「剥围栏 → literal_eval/json.loads（截断容错）→ 归一化 list」；XML 点格式需单独的属性解析，官方 notebook 未提供其可视化。
- 可视化是坐标系错位的唯一可靠检验器：相对坐标可先 thumbnail 再画框（分辨率无关），绝对坐标必须先换算；官方已知限制包括多目标 >40–50 可能无限生成、密集物体可能并框。
- `process_bbox.ipynb` 提供两条转换路径；为 Qwen3-VL 制造训练数据建议采用相对口径（`÷宽高×1000` + 夹紧 [0,1000]），COCO 的 `[x,y,w,h]` 需先转角点；微调 README 的 grounding 示例数值本身与图幅不匹配，是「先画框再训练」的活教材。

## 7. 下一步学习建议

本讲完成了「推理口径 → 解析 → 可视化 → 训练标注」的 grounding 全链路。下一讲 **u7-l4 端到端实战：自定义数据集微调与评测闭环**会把本讲生成的 `my_grounding.json`（或你自己的小任务数据）真正注册、LoRA 微调、合并导出并评测，是全手册的收官。阅读源码时建议重点温习三处：`qwen-vl-finetune/qwenvl/data/__init__.py` 的注册表（u4-l3）、`train_qwen.py` 的 LoRA 分支（u5-l7）、`evaluation/mmmu/run_mmmu.py` 的评测骨架（u6-l2）；若想继续深挖 3D 方向，可循 `convert_3dbbox` 的针孔投影阅读任意一本视觉 SLAM/多视图几何教材的相机模型章节。
