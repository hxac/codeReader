# 交互式演示与图表生成脚本

## 1. 本讲目标

这本教材仓库的"产品"是文档站点，但它的教学效果有一半来自可视化资产：`img/` 下的静态图（roofline、bank conflict、TMEM 布局……）与 `_extra/demo/` 下的 19 个交互式 HTML 演示。本讲把视角从"读图的人"切换到"造图的人"，学完后你应当能够：

1. 在本地运行 `img/scripts/` 下的 matplotlib 脚本，重新生成书中任何一张静态图，并理解"脚本即图的源码、图片是构建产物"这一工程约定。
2. 修改脚本参数（带宽、算力、行列高亮、调色板……）定制可视化，并预判输出会怎么变。
3. 拆开 `_extra/demo/` 下任一自包含 HTML 演示，说清它的交互状态机、与 `viz-base.js/css` 公共库的分工，以及它如何经 `conf.py` 的 `html_extra_path` 进入站点、再被正文以 iframe 引用。

本讲是 u1-l2（仓库结构与本地构建）的直接续篇：u1-l2 讲了 `_extra` 会被"原样拷入站点"，本讲解释这套机制的完整链路与两侧的代码实现。

## 2. 前置知识

- **matplotlib 最小集**：`figure`/`axes` 是画布与坐标系，`plt.savefig` 把图写到文件；`matplotlib.use('Agg')` 表示用无界面的离屏后端（服务器/CI 上也能跑，不弹窗）。不需要精通 matplotlib，能看懂"造数据 → 画线/画方块 → 存文件"三段即可。
- **HTML/CSS/JS 最小集**：DOM 是页面上的元素树；`document.getElementById` 取元素、`className` 切换样式类、`addEventListener` 绑事件；`postMessage` 是 iframe 与父页面之间跨文档通信的标准 API。本讲遇到的 JS 不超过"状态对象 + 重绘函数"的模式。
- **Sphinx 挂载约定（承接 u1-l2）**：`html_extra_path` 列出的目录会被**原样**拷进站点根目录；正文用 ```` ```{raw} html ```` 块内嵌任意 HTML（包括 iframe）。
- **两个被可视化的概念**：roofline 模型（u3-l1：性能上限取算力屋顶与"带宽×算术强度"的较小者）与 XOR swizzle（u4-l4：`mapped_col = col ⊕ row` 让行读、列读都无 bank 冲突）。本讲把它们当作"图里画的是什么"，不重新推导。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [img/scripts/README.md](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/README.md) | 图表生成脚本的入口说明：怎么跑、产出哪些文件、依赖什么 |
| [img/scripts/gen_roofline.py](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/gen_roofline.py) | 生成 `img/roofline.png`（B200 屋顶线图） |
| [img/scripts/gen_swizzle_conflict.py](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/gen_swizzle_conflict.py) | 生成 `img/swizzle_conflict.svg`（行写无冲突 / 列读 8-way 冲突示意图） |
| [_extra/demo/swizzle_8x8.html](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/swizzle_8x8.html) | 交互演示：8×8 矩阵并排对比"无 swizzle"与"XOR swizzle"的逐周期 bank 占用 |
| [_extra/viz-base.js](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/viz-base.js) | 所有演示共享的行为脚本：隐藏标题、按键转发、向父页面上报自身高度 |
| [_extra/viz-base.css](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/viz-base.css) | 所有演示共享的样式与调色板（按钮、面板、格子、状态色） |
| [conf.py](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/conf.py) | 英文站构建配置：`html_extra_path` 挂载 `_extra`、引入 `demo-embed.js` |
| [static/demo-embed.js](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/static/demo-embed.js) | 站点侧的"演示查看器"：给正文里的演示 iframe 套上缩放/全屏工具栏并接管尺寸 |
| [chapter_data_layout/index.md](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_data_layout/index.md) | 正文引用示例：swizzle 章节里 iframe 嵌入 `swizzle_8x8.html` 的位置 |

## 4. 核心概念与源码讲解

### 4.1 模块一：图表生成脚本——img/scripts 下的 matplotlib 产线

#### 4.1.1 概念说明

`img/` 里的每张静态图都不是手工画的，而是由 `img/scripts/` 下一个同名前缀的 Python 脚本生成：`gen_roofline.py` 产出 `roofline.png`、`gen_swizzle_conflict.py` 产出 `swizzle_conflict.svg`……**脚本是图的源码，图片是构建产物**。这套约定带来三个好处：

1. **可复现**：任何人装上 `matplotlib` 与 `numpy` 就能重出全书的图，不依赖某台机器上的绘图软件。
2. **可参数化**：图中的硬件常数（带宽、算力）、示例工作负载、高亮的行列都是脚本顶部的变量，改一个数字就能做"如果带宽减半，屋顶线怎么变"这类思想实验。
3. **可贡献**：发现图里数字过时，修的是脚本而不是图片文件，diff 可评审。

入口文档 [img/scripts/README.md:L3-L21](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/README.md#L3-L21) 给出运行方式与脚本→产物的对照表；[img/scripts/README.md:L23-L25](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/README.md#L23-L25) 声明依赖只有 `matplotlib`、`numpy`，并明确"正文引用的图片检入在 `img/`"。

注意两点（都以实际目录为准）：

- README 列了 14 个脚本，但目录下实际有 16 个——`gen_mma_m16n8k16_fragment.py` 与 `gen_nsys_b200_timeline.py` 未列入清单（分别对应 `img/mma_m16n8k16_fragment.svg`、`img/nsys_b200_timeline.svg`）。README 是入口，不是全集。
- `img/` 里还能看到若干 `_zh` 后缀的中文版图片（如 `flash_attention_main_handoff_zh.svg`），说明部分图有语言版本之分；具体由哪些脚本产出，以各脚本内部的保存路径为准。

#### 4.1.2 核心流程

所有脚本共用一个"三段式"流程：

```text
① 读常数/造数据（numpy 数组、循环构造坐标）
        ↓
② 画图（plt.plot 折线 / Rectangle 色块 / annotate 箭头标注）
        ↓
③ savefig 到 ../ 下的 img/ 目录（png 或 svg），print 一行确认
```

两个运行细节值得先记住：

- **无界面后端**：脚本开头 `matplotlib.use('Agg')`，保证在没有显示环境的服务器/CI 上也能跑。
- **输出路径有两种写法**：`gen_roofline.py` 用相对路径 `'../roofline.png'`，因此**必须**按 README 在 `img/scripts/` 目录下运行；`gen_swizzle_conflict.py` 则用 `Path(__file__).resolve().parent.parent` 从脚本自身位置推算 `img/` 目录，从任何工作目录运行都可以。

roofline 图背后的数学（u3-l1 的复述）：

\[
\text{attainable}(I) = \min\left(P_{\text{peak}},\; BW \times I\right)
\]

其中 \(I\) 是算术强度（FLOP/byte），两屋顶的交点即拐点（ridge point）：

\[
I_{\text{ridge}} = \frac{P_{\text{peak}}}{BW} = \frac{2000\ \text{TFLOP/s}}{8\ \text{TB/s}} = 250\ \text{FLOP/byte}
\]

#### 4.1.3 源码精读

**（a）gen_roofline.py：参数集中在文件头部**

[img/scripts/gen_roofline.py:L14-L16](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/gen_roofline.py#L14-L16) 定义三个核心常数：`PEAK_TFLOPS = 2000.0`（B200 稠密 fp16 Tensor Core 算力的量级值）、`BW_TB_S = 8.0`（HBM3e 带宽）、`RIDGE = PEAK_TFLOPS / BW_TB_S`。

> 读源码也要核对注释：L16 行尾注释写 `# ~281 FLOP/byte`，但按常量计算 \(2000/8 = 250\)，与文件头 docstring [img/scripts/gen_roofline.py:L7-L8](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/gen_roofline.py#L7-L8) 写的 `~250` 一致——L16 的行尾注释是过时残留。图上实际标注用的是 f-string 打印的 `RIDGE` 真实值（L25），所以图是对的，错的是那条注释。这正说明"脚本即源码"的价值：数字只算一次，注释才会撒谎。

[img/scripts/gen_roofline.py:L18-L19](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/gen_roofline.py#L18-L19) 用 `np.logspace(-1, 4.3, 500)` 在对数轴上取 500 个算术强度采样点，再对每个点取两个屋顶的较小者——这就是屋顶线折线本身。

[img/scripts/gen_roofline.py:L32-L36](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/gen_roofline.py#L32-L36) 定义三个"示例工作负载"点（标签、横坐标算术强度、纵坐标实测性能、颜色、标注偏移）：memory-bound 的 elementwise/RMSNorm、naive GEMM 4096³（算术强度很高但实测只有 2.9 TFLOP/s）、SOTA GEMM（约 2/3 峰值）。[L42-L44](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/gen_roofline.py#L42-L44) 再从 naive 点向 SOTA 点画一条绿色箭头——u3-l1 里"同样算术强度、性能差约 450 倍"那条著名结论，在图上就是这根箭头。

[img/scripts/gen_roofline.py:L52-L53](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/gen_roofline.py#L52-L53) 以 `dpi=150` 保存到相对路径 `../roofline.png`——这就是"必须在 `img/scripts/` 下运行"的原因。

**（b）gen_swizzle_conflict.py：一张图讲清 bank conflict**

这个脚本画的是 u4-l4 的核心示意图：中间一个 8×8 行主序 SMEM tile（颜色 = bank 组 = 列号），左边"写一行"命中 8 个不同 bank（无冲突），右边"读一列"全部落在同一 bank（8-way 冲突）。

[img/scripts/gen_swizzle_conflict.py:L11](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/gen_swizzle_conflict.py#L11) 用 `Path(__file__).resolve().parent.parent` 定位仓库 `img/` 目录——与 (a) 形成对照的另一种输出路径写法。[L13](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/gen_swizzle_conflict.py#L13) 的 `COLS` 列表给 8 个 bank 各配一种颜色。

[img/scripts/gen_swizzle_conflict.py:L27-L35](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/gen_swizzle_conflict.py#L27-L35) 定死版面常数并双层循环画 64 个格子，格子颜色只取 `COLS[c]`——**颜色按列号涂**，这正是"行主序下列读全撞一个 bank"的可视化根源；[L29](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/gen_swizzle_conflict.py#L29) 中 `WR_ROW = 2`、`RD_COL = 5` 指定被高亮演示的行与列，[L40-L43](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/gen_swizzle_conflict.py#L40-L43) 给它们描黑框。

左面板 [L49-L56](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/gen_swizzle_conflict.py#L49-L56)：8 个线程各写 `b0…b7` 一个格子，结论文字 "✓ conflict-free"；右面板 [L64-L72](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/gen_swizzle_conflict.py#L64-L72)：8 个线程读的格子全部用 `COLS[RD_COL]` 同色，"✗ 8-way conflict"。[L76-L78](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/gen_swizzle_conflict.py#L76-L78) 的脚注一句话给出解法：swizzle 把列 `c` 存到 `c⊕r`，让两种读法都散开。最后 [L80-L82](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/gen_swizzle_conflict.py#L80-L82) 存成 SVG（矢量格式，正文里缩放不糊）。

#### 4.1.4 代码实践

**实践 A：重出两张图并做一次参数实验**

1. **目标**：验证"脚本即图的源码"，并用参数改动做一次屋顶线思想实验。
2. **步骤**：
   ```bash
   cd img/scripts
   python gen_roofline.py            # 预期打印 Saved roofline.png，并在 ../ 生成 roofline.png
   python gen_swizzle_conflict.py    # 预期打印 wrote swizzle_conflict.svg
   ```
   然后（可选实验）把 [gen_roofline.py:L15](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/gen_roofline.py#L15) 的 `BW_TB_S` 从 8.0 改成 4.0 再跑一次；把 [gen_swizzle_conflict.py:L29](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/gen_swizzle_conflict.py#L29) 的 `RD_COL` 从 5 改成 3 再跑一次。
3. **观察什么**：带宽减半后，屋顶线的斜段斜率减半、虚线拐点横坐标从 250 移到 500（按 \(2000/4\)），而水平段不动；`RD_COL=3` 后右面板八个格子的颜色从蓝系换成绿系、文字从 "all bank 5" 变成 "all bank 3"，冲突结论不变。
4. **预期结果**：图片重新生成且与上述预判一致（以上均由源码常量直接推出，属预期而非我已运行的结果；具体渲染效果待本地验证）。
5. **重要提醒**：这些脚本会**直接覆盖 `img/` 下检入仓库的图片**。实验后务必 `git restore img/`（或 `git checkout -- img/`）恢复原图，再决定是否真的要提交改动；`git diff --stat img/` 可以先确认改了哪些文件。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `gen_roofline.py` 必须在 `img/scripts/` 目录下运行，而 `gen_swizzle_conflict.py` 不用？
**答案**：前者用相对路径 `'../roofline.png'` 保存，路径相对当前工作目录解析；后者在 [L11](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/gen_swizzle_conflict.py#L11) 用 `Path(__file__).resolve().parent.parent` 从脚本文件自身位置推算 `img/` 目录，与工作目录无关。

**练习 2**：把 `PEAK_TFLOPS` 从 2000 改成 1000，图上哪些元素会变、哪些不变？
**答案**：水平虚线（算力屋顶）高度减半；拐点 `RIDGE` 从 250 变为 \(1000/8=125\)，竖直虚点线右移、其旁标注文字随 f-string 更新；屋顶折线的水平段下移。三个工作负载点的坐标是手工填的常数（naive 2.9、SOTA 1320 TFLOP/s），不会自动变——这正是"示意图"与"计算图"的边界：屋顶是算出来的，样例点是标上去的。

**练习 3**：想把 `gen_swizzle_conflict.py` 的输出从 SVG 换成 PNG，改哪一行？
**答案**：[L80](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/gen_swizzle_conflict.py#L80) 的 `fig.savefig(f"{OUT}/swizzle_conflict.svg", ...)` 改成 `.png` 后缀即可；但正文 `chapter_*` 引用的是 SVG 文件名，只改脚本会让正文图失链，需同步改引用（这也演示了"产物文件名是脚本与正文之间的契约"）。

### 4.2 模块二：交互演示资产——_extra/demo 下的自包含 HTML

#### 4.2.1 概念说明

`_extra/demo/` 下有 19 个 HTML 文件（`swizzle_8x8`、`swizzle_128B`、`mbarrier_mechanism`、`phase_tracking`、`tcgen05_intro`、`tma_intro`、`tirx_dispatch`……），几乎每一个都对应书里一个"光靠静态图讲不动"的动态概念：逐周期的 bank 占用、phase 翻转、pipeline 重叠。它们的共同设计是**自包含（self-contained）**：

- 一个文件就是全部：结构、样式覆盖、交互逻辑都写在一个 HTML 里，没有构建步骤、没有 npm 依赖、没有外部 CDN；
- 公共部分外置两层：共享样式与调色板放 `_extra/viz-base.css`，共享行为放 `_extra/viz-base.js`，演示只用一行 `<link>`/`<script>` 引用（见 [swizzle_8x8.html:L6-L7](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/swizzle_8x8.html#L6-L7)）；
- 既能被站点的正文 iframe 嵌入（4.3 讲挂载机制），也能在文件管理器里双击直接打开——本地实验零环境。

中文站有自己的镜像：`zh/_extra/demo_zh/` 存放汉化版演示，中文正文引用 `../demo_zh/...`（如 [zh/chapter_data_layout/index.md:L388](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/zh/chapter_data_layout/index.md#L388)）。

#### 4.2.2 核心流程

以 `swizzle_8x8.html` 为例，交互演示普遍采用"**状态对象 + 纯重绘函数**"的模式：

```text
状态 ST {t: 'col'|'row',   ← 读列还是读行
         i: 索引,           ← 读第几列/第几行（-1 = 未选）
         c: 当前周期,       ← 0 = All
         pl: 播放中}
   │
   ├─ getCells()      由 ST 算出本次被读的 8 个格子坐标
   ├─ getCycles(set, sw)  把这 8 个格子按 bank 分组；
   │                      周期数 = 最大 bank 多重度（冲突重数）
   └─ draw()          用同一份 ST 画两个面板：
        sw=0（无 swizzle，bank = 列号）
        sw=1（XOR swizzle，bank = 列号⊕行号）
        外加：bank 活动条、逐周期时间线、图例
```

所有按钮、点击、键盘事件的处理器都只做一件事——改 `ST` 再调 `draw()`，整个页面没有别的更新路径。这样任何交互效果都可以从"状态怎么变"推出来。

#### 4.2.3 源码精读

**（a）XOR 模型三行核心**：[_extra/demo/swizzle_8x8.html:L118-L123](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/swizzle_8x8.html#L118-L123) 定义了演示的全部数学：

- `bank(r,c,sw)`：无 swizzle 时 bank 就是列号 `c`，有 swizzle 时是 `c^r`；
- `physCol(r,c,sw)` 与 `logAtPhys(r,p,sw)`：物理列位置与逻辑列号互查，两者用的是**同一条** `^r` 公式——u4-l4 讲的 XOR swizzle 自反性（读写两端共用一条公式）在代码里就是这两个函数互为逆。

**（b）周期数怎么算**：[_extra/demo/swizzle_8x8.html:L131-L139](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/swizzle_8x8.html#L131-L139) 的 `getCycles` 把被读集合按 bank 分桶，`mx = ` 各桶大小的最大值即总周期数；第 `ci` 个周期输出"每个桶里的第 `ci` 个元素"。列读 8 个元素、无 swizzle 时全进同一个桶（`mx=8`，8-way 冲突），有 swizzle 时 8 个桶各一个（`mx=1`，单周期完成）。

**（c）双面板与"物理布局"网格**：`draw()` 在 [_extra/demo/swizzle_8x8.html:L156-L157](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/swizzle_8x8.html#L156-L157) 用 `for(let sw=0;sw<2;sw++)` 一次画出"Without Swizzle / With Swizzle (XOR)"两个面板（面板容器声明在 [L90-L100](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/swizzle_8x8.html#L90-L100)）。网格绘制在 [L173-L190](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/swizzle_8x8.html#L173-L190)：横轴是**物理列位置**，格子颜色按 `BK[p%8]`（物理位置的 bank），格子里显示的数字是 `logAtPhys(r,p,swz)` 反查回来的**逻辑列号**——swizzle 面板里"每行数字乱序但颜色呈对角花纹"的效果由此而来。

**（d）bank 活动条与逐周期时间线**：[L193-L202](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/swizzle_8x8.html#L193-L202) 渲染 8 个 bank 槽位，某 bank 被多个读命中就标 `×N` 并加红色冲突描边；[L221-L270](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/swizzle_8x8.html#L221-L270) 的 `drawTimeline` 把每个周期哪些 bank 同时活跃画成可点击的行，[L274-L279](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/swizzle_8x8.html#L274-L279) 的 `doPlay` 用 `setInterval` 每 700ms 推进一个周期自动播放。

**（e）点击连线和键盘**：[L358-L368](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/swizzle_8x8.html#L358-L368) 点击任一格子会在两个面板里同时高亮**同一个逻辑元素**并画一条贝塞尔箭头（"swizzle mapped location"），直观展示同一数据搬到了哪里；[L374-L379](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/swizzle_8x8.html#L374-L379) 支持左右方向键切周期、空格播放。

#### 4.2.4 代码实践

**实践 B：手推演示的核心计算，再用页面核对**

1. **目标**：证明你看懂了 `getCycles`，并把 u4-l4 的理论结论（列读 8 周期 → 1 周期）与本演示逐格对应。
2. **步骤**：
   - 手算：选中"Read Col"、索引 5。无 swizzle 时 8 个格子 `(0,5)…(7,5)` 的 bank 全是 5（`bank=c`）→ 一个桶装 8 个 → 8 个周期、8-way 冲突。有 swizzle 时 bank 依次为 \(5⊕r\)，\(r=0..7\)：`5,4,7,6,1,0,3,2`——恰好取遍 0..7 → 8 个桶各 1 个 → 1 个周期。
   - 打开页面核对：直接双击 `_extra/demo/swizzle_8x8.html`（无需构建站点），选 Read Col = 5，观察左面板 "✗ 8-way conflict"、右面板 "✓ No conflicts"，再点几个周期按钮与 ▶ 播放。
   - 用点击连线验证映射：点右面板第 0 行的格子 `5`，看它连到左面板（无 swizzle）中同一逻辑列的位置。
3. **观察什么**：两个面板的周期总数（8 vs 1）、bank 活动条上 `B5 ×8` 的红色标记、时间线里"每周期仅 1/8 bank 活跃"对"8 bank 全活跃"。
4. **预期结果**：与手算完全一致；若不一致，优先检查你是否把"横轴=物理位置、数字=逻辑列"看反了（这是最容易读反的一处，代码注释见 [L173-L174](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/swizzle_8x8.html#L173-L174)）。
5. 页面交互效果与浏览器相关，具体渲染待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：演示里同时显示"格子的颜色"和"格子里的数字"，各代表什么？
**答案**：颜色 = 该**物理位置**所属的 bank（`BK[p%8]`，按物理列取色）；数字 = 存放在该物理位置的**逻辑列号**（`logAtPhys(r,p,swz)` 反查）。无 swizzle 时两者一致，有 swizzle 时数字每行乱序、颜色花纹保持规律。

**练习 2**：把"Read Row"选为索引 2，两个面板各需要几个周期？
**答案**：都是 1 个周期。读第 `r` 行的 8 个元素时无 swizzle 的 bank 是 `0..7`（各不相同）；有 swizzle 时是 `c^r`，`c=0..7` 与固定值 `r` 异或后仍是 0..7 的一个排列，仍各不相同。这正是"行主序天然利于行读、swizzle 补上列读"的另一半：**swizzle 不破坏原本无冲突的一方**。

**练习 3**：为什么这个演示只需要 `bank = c` / `c^r` 这么简化的模型，而不模拟真实的 `bank = (addr//4) % 32`？
**答案**：演示声明了模型假设——页首副标题写明 "8×8 matrix, 1 element = 1 bank"（[L78](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/swizzle_8x8.html#L78)）。8×8、每元素一 bank 时 `c` 与 `c^r` 的行为与真实 32-bank、XOR 作用于 sector 的模式**同构**（u4-l4 与 `swizzle_128B.html` 讲真实版本），教学演示取最小可同构模型，`swizzle_atom_general.html` 等姊妹演示再补真实宽度。

### 4.3 模块三：viz-base 公共库与正文挂载机制

#### 4.3.1 概念说明

19 个演示若各自维护按钮样式、调色板、iframe 高度上报，会立刻失控。仓库的做法是把**共性**抽到两个公共文件：

- `_extra/viz-base.css`：定义 CSS 变量形式的统一调色板（8 个组色 `--color-group-0..7`、强调色、好/坏状态色）与基础组件类（`.controls`/`.btn` 按钮组、`.panels`/`.panel` 双面板、`.grid` 格子），还有 `body.notitle`、`body.figure` 两个隐藏标题的模式类。各演示再用自己的 `<style>` 做**覆盖式**定制（如 [swizzle_8x8.html:L8-L72](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/demo/swizzle_8x8.html#L8-L72) 里 swizzle 特有的 bank 条与时间线样式）。注意到 `swizzle_8x8.html` 的 8 个 bank 颜色 `BK` 数组与 viz-base.css 的组色是一致的——JS 侧画格子、CSS 侧画图例用同一套色值。
- `_extra/viz-base.js`：所有演示共享的三种行为——URL 参数模式、按键转发、自动上报高度（下详）。

而**正文挂载**是三方协作：Sphinx 构建配置把 `_extra` 原样拷进站点根，正文 Markdown 用 `{raw} html` 块写 iframe，站点侧的 `static/demo-embed.js` 再给这些 iframe 套上可缩放的"查看器"。三方只通过 URL 路径与 `postMessage` 消息协作，互相不知道对方实现。

#### 4.3.2 核心流程

**挂载链**（英文站）：

```text
_extra/  ──(conf.py: html_extra_path=["_extra"])──▶  站点根 /
    ├── demo/swizzle_8x8.html                        ← iframe src="../demo/…"
    ├── viz-base.css                                 ← 演示内 <link href="../viz-base.css">
    └── viz-base.js                                  ← 演示内 <script src="../viz-base.js">

正文 chapter_*/index.md ──(```{raw} html + <iframe>)──▶ 页面里的 iframe
站点页面 ──(html_js_files 引入 demo-embed.js)──▶ 给 iframe 套缩放工具栏
```

中文站（[zh/conf.py:L42-L44](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/zh/conf.py#L42-L44)）的 `html_extra_path = ["../_extra", "_extra"]` 同时拷入根 `_extra`（得到 `/demo/`）与 `zh/_extra`（得到 `/demo_zh/`），所以中文正文可以引用 `../demo_zh/swizzle_8x8.html`。

**自动高度（push-based）**：iframe 默认高度写死在正文的 inline style 里，而演示内容会随点击展开/收起。解决方案是"**演示自己量自己、主动上报**"：

```text
演示内部 (viz-base.js)
  ResizeObserver / MutationObserver / click / 延时兜底
      ──▶ 读 body.scrollHeight（可增可减）
      ──▶ postMessage({type:'demoHeight', height}, parent)
父页面 (demo-embed.js)
  window message 监听器按 e.source 匹配到对应 iframe
      ──▶ iframe._setNatHeight(h) 更新高度并重算缩放
```

为什么从内部推而不是父页面从外面观察？viz-base.js 的注释给出了理由：演示自己最能捕捉"一次点击追加了一行、展开了一个面板"这类 DOM 变化，外面盯着 iframe 的 `<body>` 会漏；且用 `body.scrollHeight`（而非被视口地板截断的 `documentElement.scrollHeight`）才能让高度**既能涨也能缩**。

#### 4.3.3 源码精读

**（a）viz-base.js 的三段共享行为**：

- [_extra/viz-base.js:L2-L5](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/viz-base.js#L2-L5) 解析 URL 参数：演示被嵌入时加 `?notitle` 即隐藏 `<h1>` 与副标题（配合 viz-base.css 的 `body.notitle` 规则），让正文里只露出图形主体。
- [_extra/viz-base.js:L6-L14](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/viz-base.js#L6-L14) 当演示在 iframe 里（`window.parent !== window`）时，把方向键/Esc/空格转发给父页面——注释说明这是为 reveal.js 幻灯片场景准备的，说明这套演示先服务幻灯片、后被书站复用。
- [_extra/viz-base.js:L24-L54](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/viz-base.js#L24-L54) 是自动高度 IIFE：`report()`（[L27-L34](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/viz-base.js#L27-L34)）读 `body.scrollHeight` 且只在变化超过 1px 时上报；[L43-L48](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/viz-base.js#L43-L48) 挂 ResizeObserver 与 MutationObserver 兜住一切 DOM 变化；[L52-L54](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/viz-base.js#L52-L54) 再用点击捕获与 100/300/600/1200ms 定时器兜住异步与字体加载等"晚沉降"。

**（b）conf.py 的挂载点**：[conf.py:L48-L52](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/conf.py#L48-L52) 三行配置——`html_extra_path = ["_extra"]`（整目录原样拷入站点根，注释明确写了"self-contained HTML+CSS+JS copied verbatim into the site root, then embedded via <iframe>"）、`html_css_files` 引入 `demo-embed.css`、`html_js_files` 引入 `demo-embed.js`。注意关键一点：`html_extra_path` 拷贝时**保留目录结构**，所以演示里的相对引用 `../viz-base.css` 在源码树（`_extra/demo/` → `_extra/`）与在站点（`/demo/` → 站点根）**解析结果相同**——这是整套相对路径设计成立的前提。

**（c）正文如何引用**：[chapter_data_layout/index.md:L484-L487](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_data_layout/index.md#L484-L487) 用 ```` ```{raw} html ```` 块写了一个 `loading="lazy"` 的 iframe，src 是相对路径 `../demo/swizzle_8x8.html`（相对于 `chapter_data_layout/index.html` 页面解析到 `/demo/…`），inline style 给了 640px 初始高度；[L489-L490](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_data_layout/index.md#L489-L490) 紧跟一段 prose 告诉读者点哪个列索引、两个布局分别几个周期——**图负责探索，文字负责引导**。

**（d）站点侧查看器 demo-embed.js**：文件头注释 [static/demo-embed.js:L1-L6](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/static/demo-embed.js#L1-L6) 说明动机：演示按约 1200–1300px 宽设计，塞进窄的正文栏会变小，所以包一层"默认适配栏宽 + +/- 反复缩放 + 滚动平移"的视口。[L26-L46](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/static/demo-embed.js#L26-L46) 的 `setup()` 在 iframe 原位插入工具栏/视口节点并把 iframe 挪进去，同时**剥掉正文里的 inline 尺寸**改由自己接管；[L50-L62](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/static/demo-embed.js#L50-L62) 用 `transform: scale()` 而非 CSS `zoom` 缩放——注释解释 Safari 不把 `zoom` 应用到 iframe 内容；[L141-L156](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/static/demo-embed.js#L141-L156) 的 message 监听器把 `demoHeight` 消息按 `e.source` 路由给对应 iframe，`init()` 则用选择器 `iframe[src*="/demo/"], iframe[src*="/demo_zh/"]` 找到所有演示 iframe——**约定是 URL 路径里含 `/demo/`**，正文只要照这个路径写 src 就自动获得整套查看器，无需逐个登记。

#### 4.3.4 代码实践

**实践 C：走通挂载链，验证 `?notitle` 与查看器**

1. **目标**：亲眼看到"三方只在路径与消息上耦合"。
2. **步骤**：
   - 直接用浏览器打开 `_extra/demo/swizzle_8x8.html`，再打开 `_extra/demo/swizzle_8x8.html?notitle` 对比。
   - 按 u1-l2 的三步本地构建站点（`pip install -r requirements-docs.txt` → `sphinx-build -b html . _build/html` → `python -m http.server -d _build/html 8000`），访问 swizzle 章节页面，找到正文内嵌的演示。
   - 在浏览器开发者工具的 Network 面板确认页面请求了 `/demo/swizzle_8x8.html`、`/viz-base.css`、`/viz-base.js`；在 Console 里临时 `window.addEventListener('message', e => console.log(e.data))` 后点击演示里的周期按钮，观察 `demoHeight` 消息。
3. **观察什么**：独立打开时页面有 `<h1>` 标题、URL 加 `?notitle` 后标题消失（viz-base.js L2-L5 + css 的 `body.notitle` 规则）；站点内演示外层出现 `− / ⛶ / +` 工具栏、点 `+` 可放大、`⛶` 进全屏；iframe 高度随点击展开的内容自动变化。
4. **预期结果**：`demoHeight` 消息只在内容高度变化超过 1px 时出现（viz-base.js L30 的 `Math.abs(h - lastH) > 1` 去抖）；`?notitle` 生效。独立打开（`window.parent === window`）时自动高度模块直接 return（[L25](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/_extra/viz-base.js#L25)），不会向任何人发消息。浏览器端行为待本地验证。
5. 如果本地构建失败，回到 u1-l2 检查依赖安装；挂载机制本身可通过直接读 `_build/html/` 下是否出现 `demo/`、`viz-base.js` 副本验证。

#### 4.3.5 小练习与答案

**练习 1**：正文 iframe 里写的是 `src="../demo/swizzle_8x8.html"`，演示文件里写的是 `href="../viz-base.css"`。两个 `..` 分别相对谁解析？为什么在源码树和站点里都能工作？
**答案**：前者相对**章节页面**（`chapter_data_layout/index.html` → 站点根下的 `demo/`），后者相对**演示页面自身**（`/demo/swizzle_8x8.html` → 站点根下的 `viz-base.css`）。因为 `html_extra_path` 拷贝 `_extra` 时保留内部目录结构（`demo/` 与两个 viz-base 文件的相对位置不变），源码树里 `_extra/demo/` → `_extra/` 的相对关系被原样映射到站点 `/demo/` → 根。

**练习 2**：为什么高度上报要演示"从里面推"（postMessage），而不是 demo-embed.js"从外面"观察 iframe 高度？
**答案**：外面只能看到 iframe 的盒子尺寸，盒子恰恰是要被设置的目标，形成"量自己设的值"的循环；而演示内部能观察到自己的 DOM 变化（点击追加行、展开面板），配合 `body.scrollHeight` 可以及时且可涨可缩地得到真实内容高度。demo-embed.js 仍保留了同源 ResizeObserver 兜底（[L82-L93](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/static/demo-embed.js#L82-L93)），但主路径是 push。

**练习 3**：若想新增一个演示并在正文使用，最少要动哪几处？
**答案**：①在 `_extra/demo/` 放一个自包含 HTML（引 `../viz-base.css`、`../viz-base.js`，套用 `.controls/.panels` 等基础类）；②在目标章节的 `index.md` 里加 ```` ```{raw} html ```` 的 iframe，src 写 `../demo/<名字>.html`（路径含 `/demo/` 即自动获得缩放查看器）。**不需要**改 conf.py、不需要登记清单——挂载是按目录约定与 URL 模式自动生效的。若还需静态图，则在 `img/scripts` 加 `gen_*.py` 并把产物检入 `img/`（同时更新该目录 README 的对照表，避免它继续偏离全集）。

## 5. 综合实践

**任务：给 roofline 图增加一个工作负载点，并为一个概念搭出"最小演示骨架"。**

第一部分（改产线）：

1. 复制 `img/scripts/gen_roofline.py` 的三个工作负载点写法（[L32-L36](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/gen_roofline.py#L32-L36)），在 `pts` 列表里追加一个点，例如 "Attention 4096², d=128 — flash"：按 u3-l1 的公式 \(AI \approx L/2s\)（\(L\) 为序列长、\(s=2\) 字节 fp16）估算横坐标，纵坐标取 \(8 \times AI \times 0.8\)（80% 带宽利用率）量级。
2. 运行脚本重出图，用 `git diff img/roofline.png` 确认只有图变了；对照 u3-l1 的算子谱系检查新点落在屋顶哪一侧。
3. 实验完 `git restore img/` 恢复。

第二部分（搭骨架，建议在本地分支、不提交）：

4. 仿照 `swizzle_8x8.html` 的"状态对象 + 重绘函数"骨架，新建一个最小演示：任选一个你已经掌握的概念（如 phase 奇偶翻转：一个 2 状态的小方格，点击按钮翻转，旁边显示 `try_wait` 会放行还是阻塞），页面里只写 `<link href="../viz-base.css">`、`<script src="../viz-base.js">` 与二三十行 JS。放在 `_extra/demo/` 下即可双击打开自测，也可在本地构建后用 `{raw} html` iframe 试嵌一次，验证 `?notitle` 与缩放工具栏自动生效。
5. 产出一份清单：《若把这张图/这个演示贡献回上游，需要动哪些文件》——对照本讲源码地图，至少应包含：脚本本身、`img/`（若产物检入）、`img/scripts/README.md` 对照表、章节 `index.md` 引用处、（演示涉及中文站时）`zh/` 下的镜像文件。

## 6. 本讲小结

- `img/scripts/` 是全书静态图的"源码"：每个 `gen_*.py` 用 matplotlib + numpy 三段式（造数据→画→存）生成 `img/` 下检入的图片，改图先改脚本；README 是入口但非全集（目录实有 16 个脚本，README 列 14 个）。
- 两个实用的工程细节：`gen_roofline.py` 用相对路径保存所以必须在 `img/scripts` 下运行，`gen_swizzle_conflict.py` 用 `__file__` 推算目录所以随处可跑；注释可能与代码脱节（`RIDGE` 行尾的 `~281` 实为 250），数字以常量计算为准。
- `_extra/demo/` 的 19 个演示是自包含单文件 HTML，公共样式/行为外置到 `viz-base.css/js`，交互普遍采用"状态对象 + 纯重绘函数"模式；`swizzle_8x8.html` 的全部数学就是 `bank = c` 与 `bank = c⊕r` 两条，周期数 = bank 分桶后的最大多重度。
- 挂载机制是三方松耦合：`conf.py` 的 `html_extra_path=["_extra"]` 把演示原样拷入站点根（目录结构保留，故相对引用 `../viz-base.css` 在源码树与站点同样成立）；正文用 `{raw} html` iframe 按约定路径 `/demo/…` 引用；`static/demo-embed.js` 按 URL 模式自动给 iframe 套缩放/全屏查看器，高度由 `viz-base.js` 在演示内部量好后经 `demoHeight` 消息推送。
- 中文站靠 `zh/conf.py` 同时挂载根 `_extra`（`/demo/`）与 `zh/_extra`（`/demo_zh/`），中文正文引用 `../demo_zh/…` 的汉化镜像。

## 7. 下一步学习建议

本讲是单元十六的第一讲，收尾方向有二：

1. **进入 u16-l2（capstone 综合实战）**：把本讲的"改脚本参数、搭演示骨架"升级为完整的内核变体设计与贡献流程——你会用到本讲的贡献清单思路（哪些文件必须同步改），以及全书积累的 scope/layout/dispatch 与屏障协议知识。
2. **继续深挖可视化基建本身**（可选）：通读 `static/demo-embed.css` 看查看器的样式实现；对照 `_extra/demo/` 里更复杂的演示（`phase_tracking.html`、`mbarrier_mechanism.html` 对应 u8 的相位理论，`tirx_dispatch.html` 对应 u9-l3 的三要素），体会"同一个 viz-base 骨架如何承载完全不同的教学模型"；再用 `git log -- img/scripts/` 读几个图的历史，观察教材改图与正文修订如何同步演进。
