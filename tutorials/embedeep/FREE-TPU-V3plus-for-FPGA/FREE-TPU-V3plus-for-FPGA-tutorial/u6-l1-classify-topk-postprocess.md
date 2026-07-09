# 分类后处理：topk 排序

## 1. 本讲目标

本讲是「后处理算法」单元的第一讲。前一章（u5-l2）我们已经把 TPU 硬件输出的 epmat 反量化读成了浮点结果：Linux 路线得到 `std::vector<EEPTPU_RESULT>`，裸机路线得到 `std::vector<ncnn::Mat>`。但这些浮点数组本身还不能直接给人看——分类网络最后要回答的问题是「这张图最可能是哪几类？」。

本讲只解决一件事：**如何从一个长度等于类别数的浮点得分向量里，挑出概率最大的 k 个类别，并带上它们的类别下标**。学完后你应当掌握：

1. 把 TPU 输出的 4D/3D 张量「拍扁」成一维得分向量（输出 reshape）。
2. 用 `std::partial_sort` 配合 `(score, index)` pair 取 topk 的标准写法，并理解为什么不直接排序浮点数组。
3. 读懂两条 demo（Linux `classify` 与裸机 `standalone`）里 `get_topk` 的细微差异。
4. 在任意一台装了 g++ 的 PC 上独立编写、编译、验证 topk 后处理——因为它是纯 CPU 逻辑，不依赖 TPU。

## 2. 前置知识

- **分类网络的输出形状**：图像分类网络（如 MobileNet、ResNet）最后一层通常是一个全连接层，输出每个类别的「打分」（logit 或 softmax 后的概率）。对一个 1000 类（ImageNet）网络，输出就是 1000 个数。在张量表示里，它常被写成 4D NCHW `[1, 1000, 1, 1]`（H、W 都是 1），本质上是个一维向量。
- **topk 问题**：从 N 个数里找出最大的 k 个。这是后处理里最常见的一类问题，不只分类用得到（检测里挑高置信度框也是同构问题）。
- **C++ 标准库算法**：`std::sort`（全排序）、`std::partial_sort`（只把前 k 个排好）、`std::make_pair` / `std::pair`（把两个值绑在一起）、`std::greater`（「大于」比较器，用于降序）。
- 承接 u5-l2：到 `get_topk` 入口时，数据**已经是浮点**——Linux 侧 `EEPTPU_RESULT.data` 由 `libeeptpu_pub` 反量化好，裸机侧 `ncnn::Mat` 由 `epmat2nmat` 除以 \(2^{\text{exp}}\) 反量化好。所以本讲不再碰定点/epmat，只碰「一堆 float」。

> 本讲引用的源码全部来自以下三个文件，永久链接基于当前 HEAD `1d3b64b6`。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关注点 |
|------|------|-----------|
| `sdk/demo/classify/main.cpp` | Linux 分类 demo，含两个 `get_topk` 重载和打印 | EEPTPU_RESULT → 得分向量、`partial_sort` 取 top5、逐行打印 |
| `sdk/standalone/src/post_process/classify.cpp` | 裸机分类后处理，同样两个重载 | ncnn::Mat → 得分向量、与 Linux 版几乎相同的 `partial_sort` |
| `sdk/standalone/src/post_process/post_process.h` | 裸机后处理头文件 | 仅暴露 `get_topk(ncnn::Mat&, ...)` 一个声明 |
| `sdk/standalone/src/main.cc`（辅助） | 裸机主菜单 | 选项 2 与实时循环选项 5 里对 `get_topk` 的调用与一行式打印 |

核心结论先说在前面：**两条路线的 `get_topk`「内核」几乎逐行相同**——都是「构造 (score,index) pair 向量 → `partial_sort(greater)` → 拷出前 k 个」。真正的差异只在**入口怎么把输出张量变成一维 float 向量**（reshape）和**打印格式**上。

## 4. 核心概念与源码讲解

### 4.1 输出 reshape：从张量到一维得分向量

#### 4.1.1 概念说明

TPU 推理返回的不是「一个一维数组」，而是带形状的张量：

- Linux：`struct EEPTPU_RESULT` 里有 `data`（浮点指针）和 `shape[4]`（NCHW 四元组）。
- 裸机：`ncnn::Mat` 有 `c/h/w`（CHW，无 batch 维 N），数据按通道分块存放，要用 `result.channel(c)` 取第 c 个通道。

但 topk 算法只认「一串连续的 float」。所以第一步是 **reshape**：不管张量是几维、按什么布局存，都把它**线性化成一个 `std::vector<float> cls_scores`**，下标 i 就对应「第 i 类的得分」。对分类网络，这个长度就是类别数（如 1000）。

#### 4.1.2 核心流程

两条路线的 reshape 思路对称，区别在于「数据本来是怎么摆的」：

```
Linux (EEPTPU_RESULT, 4D NCHW, data 已是连续 float):
  total = shape[0]*shape[1]*shape[2]*shape[3]
  for i in [0, total):  cls_scores[i] = data[i]      # 直接扁平拷贝

裸机 (ncnn::Mat, 3D CHW, 按通道分块):
  total = c*h*w
  i = 0
  for c in [0, C):
      ptr = result.channel(c)                        # 第 c 个通道的首地址
      for hw in [0, H*W):
          cls_scores[i++] = *ptr++                   # 逐通道、再逐空间位置
```

由于分类输出 `H=W=1`，两个循环最终都得到长度为类别数的向量。

#### 4.1.3 源码精读

**Linux 版** reshape 在 `get_topk(EEPTPU_RESULT&, ...)` 重载里：把 `data` 按总元素数扁平拷进 `cls_scores`。

[sdk/demo/classify/main.cpp:223-236](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/classify/main.cpp#L223-L236) —— EEPTPU_RESULT 版 reshape：按 `shape[0]*shape[1]*shape[2]*shape[3]` 计算总长，逐元素拷贝；注释掉的 `if (result.h != 1 || result.w != 1) return -1;` 说明作者原本想强制「输出必须是向量」。

```cpp
cls_scores.resize(result.shape[0]*result.shape[1]*result.shape[2]*result.shape[3]);
int c = 0;
for (int i = 0; i < result.shape[0]*result.shape[1]*result.shape[2]*result.shape[3]; i++)
    cls_scores[c++] = result.data[i];   // c 与 i 始终相等，等价于 cls_scores[i]=data[i]
```

**裸机版** reshape 在 `get_topk(ncnn::Mat&, ...)` 重载里：必须按通道访问，因为 `ncnn::Mat` 内部是「通道分块」布局。

[sdk/standalone/src/post_process/classify.cpp:49-67](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/post_process/classify.cpp#L49-L67) —— ncnn::Mat 版 reshape：外层遍历通道 `c`，内层遍历该通道的空间位置 `hw`。

```cpp
for (int c = 0; c < result.c; c++) {
    float *ptrf = (float*)result.channel(c).data;
    for (int hw = 0; hw < result.h * result.w; hw++)
        cls_scores[i++] = *ptrf++;
}
```

#### 4.1.4 代码实践（源码阅读型）

1. 目标：确认两条 reshape 路径对一个 `[1,1000,1,1]` 的分类输出都得到长度 1000 的向量。
2. 步骤：打开上面两个永久链接，分别数出循环总次数。
3. 观察：
   - Linux 版：`shape[0]*shape[1]*shape[2]*shape[3] = 1*1000*1*1 = 1000`。
   - 裸机版：`result.c*result.h*result.w = 1000*1*1 = 1000`（注意 ncnn::Mat 没有 batch 维 N，所以是三项相乘）。
4. 预期结果：两者都是 1000 次写入，得到 `cls_scores.size()==1000`。

#### 4.1.5 小练习与答案

- **Q1**：为什么 Linux 用 `shape[0]*shape[1]*shape[2]*shape[3]` 四项相乘，而裸机只用 `c*h*w` 三项？
  - **A**：Linux 的 `EEPTPU_RESULT` 保留 batch 维 N，是 4D NCHW；裸机的 `ncnn::Mat` 不含 batch 维，是 3D CHW。分类任务 N=1，所以四项相乘里 `shape[0]=1` 不影响结果。
- **Q2**：如果把一个 `[1,255,13,13]` 的检测输出喂给这套 reshape，得到的向量长度是多少？它还能用 topk 分类吗？
  - **A**：长度 `1*255*13*13 = 43095`。可以算出一个 topk，但它语义上不再是「类别得分」，而是把所有 anchor、所有空间位置、所有类别的值混在一起排序了——分类后处理不适用于检测输出，检测要走 u6-l2 的框解析。

---

### 4.2 partial_sort 取 topk

#### 4.2.1 概念说明

拿到一维 `cls_scores` 后，要找最大的 k 个。最朴素的想法是 `std::sort` 全排序再取前 k 个，但那是 \(O(N\log N)\) 的全排序；我们其实不在乎第 k 名之后的相对顺序。`std::partial_sort` 专门干这事：它**只保证前 k 个元素是排好序的最大 k 个**，其余部分顺序不定，代价约为 \(O(N\log k)\)。

但有一个陷阱：**直接对 `cls_scores` 排序会丢失「分数属于哪个类别」的信息**。排序是就地重排，排完之后 `scores[i]` 已经不知道它原本在第几位（即第几类）。所以必须先把每个分数和它的原始下标绑成一个 `pair<float,int>`，再排序——排序时 pair 整体搬动，下标跟着分数走。

#### 4.2.2 核心流程

```
1. 构造 vec[i] = (cls_scores[i], i)           # 分数 + 原始类别下标
2. std::partial_sort(vec.begin(), vec.begin()+k, vec.end(), greater<pair>)
                                              # 前 k 个排成降序的「最大 k 个」
3. 把 vec[0..k) 拷进 top_list                 # top_list[i] = (第 i 大的分数, 它的类别号)
```

关键点：

- `std::greater<std::pair<float,int>>()` 让排序**降序**（默认 `partial_sort` 是升序）。
- `std::pair` 自带**字典序**比较：先比 float（分数），分数相同再比 int（下标）。所以主排序键是分数。
- 复杂度对比：全排序 \(\mathcal{O}(N\log N)\) 与 partial_sort \(\mathcal{O}(N\log k)\)。对 \(N=1000,\,k=5\)，\(\log_2 1000\approx 10\)，\(\log_2 5\approx 2.3\)，partial_sort 明显更省。

字典序「大于」的严格定义：

\[
(s_1,i_1) > (s_2,i_2) \;\Longleftrightarrow\; s_1 > s_2 \;\lor\; \bigl(s_1 = s_2 \,\land\, i_1 > i_2\bigr)
\]

即分数不同比分数，分数相同则下标大者排前（这是个无关紧要的 tie-break，分类里分数几乎不会完全相等）。

#### 4.2.3 源码精读

两个 `get_topk(const std::vector<float>&, ...)` 重载是各自文件的「内核」，几乎逐行相同。这是 Linux 版：

[sdk/demo/classify/main.cpp:198-221](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/classify/main.cpp#L198-L221) —— 接收一维得分向量的核心 `get_topk`：构造 pair、partial_sort、拷贝前 k 个。

```cpp
std::vector< std::pair<float, int> > vec;
vec.resize(size);
for (int i=0; i<size; i++)
    vec[i] = std::make_pair(cls_scores[i], i);          // 1) 绑定分数与下标

std::partial_sort(vec.begin(), vec.begin() + topk, vec.end(),
                  std::greater< std::pair<float, int> >()); // 2) 前 topk 降序

top_list.resize(topk);
for (unsigned int i=0; i<topk; i++) {
    top_list[i].first  = vec[i].first;                  // 3) 拷出分数
    top_list[i].second = vec[i].second;                 //    和类别下标
}
```

注意 `top_list[i].first = 分数`、`top_list[i].second = 类别下标`，这个约定决定了后面打印时怎么取值。

裸机版内核只在末尾多了一行 `vec.clear();`，其余完全一致：

[sdk/standalone/src/post_process/classify.cpp:23-47](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/post_process/classify.cpp#L23-L47) —— 裸机核心 `get_topk`：与 Linux 版逐行相同，仅最后多了 `vec.clear();`（[第 44 行](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/post_process/classify.cpp#L35-L36) 是同样的 `partial_sort`）。

#### 4.2.4 代码实践（可在 PC 上独立验证）

topk 后处理是纯 CPU 逻辑，**不需要 TPU**，可以在任意装了 g++ 的主机上验证。

1. 目标：给定长度 1000 的得分向量，手写 partial_sort 取 top5，并解释为何要先构造 (score,index) pair。
2. 操作步骤：把下面的「示例代码」存为 `topk_demo.cpp`，用 `g++ -O2 -std=c++11 topk_demo.cpp -o topk_demo` 编译，`./topk_demo` 运行。
3. 需要观察的现象：输出应是 5 行，每行一个 `[类别号] 分数`，分数从高到低。
4. 预期结果：我们故意把高分放在已知的下标上，便于手算验证——预期 top5 依次是类别 `3, 7, 200, 999, 0`。

示例代码（本讲义自编，非项目原文件，完全模仿项目 `get_topk` 写法）：

```cpp
// 示例代码：在 PC 上验证 topk 后处理，不依赖 TPU
#include <vector>
#include <utility>    // std::pair, std::make_pair
#include <algorithm>  // std::partial_sort
#include <cstdio>

static int get_topk(const std::vector<float>& cls_scores, unsigned int topk,
                    std::vector< std::pair<float,int> >& top_list)
{
    if (cls_scores.size() < topk) topk = cls_scores.size();   // 防御：k 不能超过总数
    int size = cls_scores.size();
    std::vector< std::pair<float,int> > vec(size);
    for (int i = 0; i < size; i++)
        vec[i] = std::make_pair(cls_scores[i], i);            // 关键：分数与下标绑定
    std::partial_sort(vec.begin(), vec.begin()+topk, vec.end(),
                      std::greater< std::pair<float,int> >()); // 前 topk 降序
    top_list.resize(topk);
    for (unsigned int i = 0; i < topk; i++) {
        top_list[i].first  = vec[i].first;                    // 分数
        top_list[i].second = vec[i].second;                   // 类别下标
    }
    return 0;
}

int main()
{
    std::vector<float> scores(1000, 0.10f);   // 默认所有类别 0.10
    // 故意把已知高分放在特定下标，便于手算验证
    scores[3]   = 0.95f;
    scores[7]   = 0.88f;
    scores[200] = 0.80f;
    scores[999] = 0.75f;
    scores[0]   = 0.70f;

    std::vector< std::pair<float,int> > top_list;
    get_topk(scores, 5, top_list);

    for (int i = 0; i < 5; i++)
        std::printf("  [%3d] %.3f\n", top_list[i].second, top_list[i].first);
    return 0;
}
```

5. 如果无法本地编译运行，明确标注「待本地验证」；但既然输入是按手算可推的方式构造的，预期输出可以确定：

```
  [  3] 0.950
  [  7] 0.880
  [200] 0.800
  [999] 0.750
  [  0] 0.700
```

**为什么必须先构造 (score, index) pair？** 三个理由：① `partial_sort` 是就地重排，单排 `float` 会丢失「分数来自第几类」，pair 让下标随分数一起搬动；② `std::pair` 自带字典序比较，配合 `std::greater` 即可降序，省去自定义比较器；③ topk 要的是「最大的 k 个值**及其类别**」，而不是「最大的 k 个值」，所以类别下标必须和分数一起进排序。

#### 4.2.5 小练习与答案

- **Q1**：把 `std::partial_sort(..., greater<pair>())` 换成 `std::sort(..., greater<pair>())`，结果会变吗？代价会变吗？
  - **A**：结果的前 k 个**不变**（仍是最大的 k 个、降序），但 `sort` 是 \(O(N\log N)\) 全排序，而 `partial_sort` 是 \(O(N\log k)\)，对 \(N\gg k\) 时 partial_sort 更快。
- **Q2**：如果只想要**最大那一个**类别，还有更简单的写法吗？
  - **A**：可以用 `std::max_element`，返回最大值的迭代器，`it - cls_scores.begin()` 即类别下标，\(O(N)\) 且无需 pair。只有需要「前 k 个」时才值得用 pair + partial_sort。
- **Q3**：`greater<pair<float,int>>` 在两个分数完全相等时怎么决定先后？
  - **A**：字典序退到比 int 下标，下标大者排前（见上面公式）。分类里几乎不会出现完全相等的分数，所以无实际影响。

---

### 4.3 结果打印与输出

#### 4.3.1 概念说明

拿到 `top_list`（一个 `(分数, 类别下标)` pair 数组）后，要把它打印成人能读的结果。注意 pair 的字段约定：`first=分数`、`second=类别下标`，所以 printf 时 `%d` 取 `second`、`%f` 取 `first`，别取反。两条 demo 的打印**格式不同**，但读取 pair 的方式一致。

#### 4.3.2 核心流程

```
topk 值的确定:
  Linux : topk 是变量(默认 5)，并按输出元素数 clamp
  裸机  : 直接硬编码 5

打印:
  Linux : 逐行  "  [%3d] %.3f"        →  每行一个 "[类别] 分数"
  裸机  : 一行   "Classify: Result (top 5): [5 个类别] [5 个分数]"
```

#### 4.3.3 源码精读

**Linux 打印**：先确定 topk（带防御性 clamp），再调用 `get_topk`，最后逐行打印。

[sdk/demo/classify/main.cpp:173-183](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/classify/main.cpp#L173-L183) —— Linux：topk 带 clamp + 逐行打印。

```cpp
int topk = 5;
if (result[0].shape[1]*result[0].shape[2]*result[0].shape[3] < topk)
    topk = result[0].shape[1]*result[0].shape[2]*result[0].shape[3];   // k 不能超过类别数
ret = get_topk(result[0], topk, top_list);
...
for (int i = 0; i < topk; i++)
    printf("  [%3d] %.3f\n", top_list[i].second, top_list[i].first);   // second=类别, first=分数
```

> 细究：这里 clamp 用 `shape[1]*shape[2]*shape[3]`（少乘了 `shape[0]`），而内核 `get_topk(vector&)` 自己也有一个 `if (cls_scores.size() < topk)` 的 clamp（[main.cpp:200](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/classify/main.cpp#L200)）。即「双重保险」，对 N=1 的分类输出二者等价。

**裸机打印**：在 `main.cc` 里，topk 固定传 5，结果用一行 printf 同时打出 5 个类别和 5 个分数。

[sdk/standalone/src/main.cc:400-409](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L400-L409) —— 裸机菜单选项 2（单次 forward）的分类后处理与一行式打印（实时循环选项 5 里 [main.cc:584-593](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L584-L593) 是完全相同的代码）。

```cpp
std::vector< std::pair<float, int> > top_list;
ret = get_topk(outputs[0], 5, top_list);                                   // topk 硬编码 5
...
printf("Classify: Result (top 5): [%d %d %d %d %d] [%f %f %f %f %f]\n",
       top_list[0].second, top_list[1].second, top_list[2].second, top_list[3].second, top_list[4].second,
       top_list[0].first,  top_list[1].first,  top_list[2].first,  top_list[3].first,  top_list[4].first);
```

注意 `outputs[0]` 是 `ncnn::Mat`（u5-l2 的 `read_forward_result` 产物），所以这里调的是 `classify.cpp` 里 `get_topk(ncnn::Mat&, ...)` 那个重载，声明在 [post_process.h:24](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/post_process/post_process.h#L24)。

#### 4.3.4 代码实践（源码阅读型）

1. 目标：确认 pair 字段约定，并理解两种打印顺序。
2. 步骤：对照 [main.cpp:182](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/classify/main.cpp#L180-L183) 与 [main.cc:405-407](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L405-L407)。
3. 观察：两处都是先取 `top_list[i].second`（下标）配 `%d`/`%3d`，再取 `top_list[i].first`（分数）配 `%f`/`%.3f`。
4. 预期结果：能说清「`.first` 是分数、`.second` 是类别号」，并指出 Linux 是竖排、裸机是一行（先 5 个类别再 5 个分数）。

#### 4.3.5 小练习与答案

- **Q1**：`top_list[0]` 一定是概率最大的那个类别吗？
  - **A**：是。`partial_sort` 配 `greater` 后 `vec[0]` 是最大值，拷进 `top_list[0]`，所以 `top_list[0]` 是 top-1。
- **Q2**：把裸机 printf 里的 `top_list[0].second` 误写成 `top_list[0].first`，会发生什么？
  - **A**：`%d` 会去读一个 float 的位模式当整数打印，结果是垃圾值——这正是 pair 字段约定必须记牢的原因。

---

### 4.4 Linux demo 与裸机实现对照

#### 4.4.1 概念说明

同一个 topk 算法，在两条部署路线里被「复制粘贴」成两份几乎相同的代码。理解它们的差异，有助于你在移植（换输出容器、换 STL 实现）时不踩坑。这一节把差异列成一张表，作为本讲的总收口。

#### 4.4.2 核心流程

两条 `get_topk` 都采用「**两层重载**」结构：

```
外层重载(张量→向量):   把 EEPTPU_RESULT / ncnn::Mat reshape 成 vector<float>
                        ↓ 调用
内核重载(向量→topk):   pair + partial_sort，两条路线几乎逐行相同
```

差异几乎全部集中在外层 reshape；内核是同一份算法。

#### 4.4.3 源码精读（差异对照表）

对照 [Linux `get_topk` 两个重载](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/classify/main.cpp#L198-L236) 与 [裸机 `classify.cpp` 两个重载](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/post_process/classify.cpp#L23-L67)：

| 维度 | Linux demo (`classify/main.cpp`) | 裸机 (`post_process/classify.cpp`) |
|------|----------------------------------|------------------------------------|
| 外层输入容器 | `struct EEPTPU_RESULT&`（`data` + `shape[4]`） | `ncnn::Mat&`（`c/h/w` + `channel()`） |
| reshape 写法 | 按 `data[i]` 扁平拷贝，循环 `shape[0]*1*2*3` 次 | 外层通道 `c`、内层空间 `hw`，逐通道读取 |
| 容器实现 | 标准库 `std::vector` | 自实现 `simplestl` 的 `std::vector`（见 u8-l3） |
| 内核算法 | pair + `partial_sort(greater)` | **完全相同** |
| 内核收尾 | 不 `clear` | 多一行 `vec.clear();` |
| topk 取值 | 变量，默认 5，按输出规模 clamp | 硬编码 5 |
| 打印格式 | 逐行 `[idx] score` | 一行：5 个 idx 再 5 个 score |
| 头文件声明 | `static`，仅本文件可见 | `post_process.h` 导出，供 `main.cc` 调用 |

一个值得注意的工程细节：裸机版多处显式 `clear()`（`vec.clear()`、`cls_scores.clear()`），而 Linux 版没有。这是因为裸机跑在**长期不重启的 `while` 菜单/实时循环**里（见 u4-l1），更在意及时归还内存；Linux demo 是一次性进程，结束即退出，无所谓泄漏。这与 u2-l2 提到的「覆盖式调用会泄漏旧副本，demo 可容忍、实时流不可」是同一条工程取舍。

#### 4.4.4 代码实践（源码阅读型）

1. 目标：亲手核对「内核几乎逐行相同」这一结论。
2. 步骤：并排打开 [Linux 内核 main.cpp:198-221](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/classify/main.cpp#L198-L221) 和 [裸机内核 classify.cpp:23-47](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/post_process/classify.cpp#L23-L47)，逐行比对。
3. 观察：除裸机末尾 `vec.clear();` 一行外，其余字符级一致。
4. 预期结果：能指出「唯一的实质差异是裸机多了 `vec.clear()`」，并解释这是长期循环 vs 一次性进程的内存取舍。

#### 4.4.5 小练习与答案

- **Q1**：如果把分类输出容器从 `EEPTPU_RESULT` 换成自定义结构，需要改哪一层？
  - **A**：只改**外层 reshape 重载**（把新结构拍扁成 `vector<float>`）；内核 `get_topk(vector&, ...)` 完全不动。这正是两层重载设计的好处——算法与数据解耦。
- **Q2**：裸机版为什么不直接 `#include <vector>` 用标准库？
  - **A**：裸机 standalone 是无操作系统的工程，BSP 不一定提供完整 C++ 标准库运行时，故自带精简 `simplestl`（详见 u8-l3）。这也解释了为什么两份代码「长得一样但实现不一样」。

---

## 5. 综合实践

把本讲四个模块串起来，完成一个**端到端的、可在 PC 上跑通的 topk 后处理小工具**（不依赖 TPU）。

**任务**：模拟一条完整的分类后处理流水线——构造一个模拟的网络输出张量，按 Linux/裸机两种方式 reshape，分别取 top5，打印并相互对照。

**操作步骤**：

1. 把下面「示例代码」存为 `classify_post_sim.cpp`，`g++ -O2 -std=c++11 classify_post_sim.cpp -o sim && ./sim`。
2. 程序里：
   - 用 `std::vector<float>` 模拟 1000 类的扁平输出（对应 Linux 的 `EEPTPU_RESULT.data`）。
   - 用一个「按通道分块」的 `ncnn::Mat` 简化替身（一个 `std::vector<std::vector<float>>`，外层通道、内层 1 个空间值）模拟裸机的 `ncnn::Mat`。
   - 对两种输入各跑一次项目同款 `get_topk`。
3. 在高分下标上放已知值，便于手算对照。

**预期结果**：两条路径打印的 top5 完全一致（类别 `3,7,200,999,0`，分数 `0.95,0.88,0.80,0.75,0.70`）——这验证了「不同 reshape 入口、相同内核」得到相同结果。

示例代码（本讲义自编）：

```cpp
#include <vector>
#include <utility>
#include <algorithm>
#include <cstdio>

// === 内核：与项目 get_topk(vector&) 逐行一致 ===
static int get_topk(const std::vector<float>& cls_scores, unsigned int topk,
                    std::vector< std::pair<float,int> >& top_list)
{
    if (cls_scores.size() < topk) topk = cls_scores.size();
    int size = cls_scores.size();
    std::vector< std::pair<float,int> > vec(size);
    for (int i = 0; i < size; i++) vec[i] = std::make_pair(cls_scores[i], i);
    std::partial_sort(vec.begin(), vec.begin()+topk, vec.end(),
                      std::greater< std::pair<float,int> >());
    top_list.resize(topk);
    for (unsigned int i = 0; i < topk; i++) {
        top_list[i].first  = vec[i].first;
        top_list[i].second = vec[i].second;
    }
    return 0;
}

static void print_top(const char* tag, std::vector< std::pair<float,int> >& tl) {
    std::printf("%s: [%d %d %d %d %d] [%f %f %f %f %f]\n", tag,
        tl[0].second, tl[1].second, tl[2].second, tl[3].second, tl[4].second,
        tl[0].first,  tl[1].first,  tl[2].first,  tl[3].first,  tl[4].first);
}

int main()
{
    const int N = 1000;
    // --- 路线 A：扁平 float 数组（模拟 Linux EEPTPU_RESULT.data, shape=[1,1000,1,1]）---
    std::vector<float> flat(N, 0.10f);
    flat[3]=0.95f; flat[7]=0.88f; flat[200]=0.80f; flat[999]=0.75f; flat[0]=0.70f;

    // --- 路线 B：按通道分块（模拟裸机 ncnn::Mat, c=1000,h=w=1）---
    std::vector< std::vector<float> > mat(N, std::vector<float>(1, 0.10f));
    mat[3][0]=0.95f; mat[7][0]=0.88f; mat[200][0]=0.80f; mat[999][0]=0.75f; mat[0][0]=0.70f;

    // A) Linux 式 reshape：扁平拷贝
    std::vector<float> clsA(N);
    for (int i = 0; i < N; i++) clsA[i] = flat[i];

    // B) 裸机式 reshape：外层通道、内层空间
    std::vector<float> clsB; clsB.reserve(N);
    for (int c = 0; c < N; c++)
        for (int hw = 0; hw < 1; hw++)
            clsB.push_back(mat[c][hw]);

    std::vector< std::pair<float,int> > topA, topB;
    get_topk(clsA, 5, topA);
    get_topk(clsB, 5, topB);

    print_top((char*)"Linux  ", topA);
    print_top((char*)"standalone", topB);
    return 0;
}
```

**思考延伸**（可选）：

- 把 `get_topk` 里的 `partial_sort` 换成 `sort`，确认 top5 不变，但用 `gettimeofday` 计时（参考 [main.cpp:238-243](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/classify/main.cpp#L238-L243) 的 `get_current_time`）对比两者耗时差异（N=1000 差异很小，把 N 调到 100 万更明显）。
- 若无法本地编译，明确标注「待本地验证」。

## 6. 本讲小结

- 分类后处理的本质是 **topk**：从「类别数」个浮点得分里挑出最大的 k 个，并带上类别下标。
- 第一步 **reshape**：Linux 把 `EEPTPU_RESULT`（4D NCHW 的 `data`）扁平拷成 `vector<float>`；裸机按 `ncnn::Mat` 的通道分块（外层 `c`、内层 `hw`）拍扁。两者都得到一维得分向量。
- 第二步 **partial_sort**：先 `make_pair(分数, 下标)` 绑定，再 `std::partial_sort(..., greater<pair>)` 取前 k 个。绑 pair 是为了排序时不丢失「分数属于哪一类」。
- 两条路线的**内核几乎逐行相同**，差异只在 reshape 入口、容器实现（标准库 vs `simplestl`）、topk 取值（变量+clamp vs 硬编码 5）、打印格式与是否 `clear()`。
- topk 是**纯 CPU 逻辑**，与 TPU 无关，可在 PC 上独立开发与验证；到 `get_topk` 入口时数据已是浮点（反量化在 u5-l2 完成）。
- 字段约定易错点：`pair.first = 分数`、`pair.second = 类别下标`，printf 时别取反。

## 7. 下一步学习建议

- 本讲只处理了「一维得分向量」的分类输出。下一讲 **u6-l2（目标检测后处理：解析框与画框）** 将处理 `[1,255,13,13]` 这种带空间结构的检测输出——每行 6 个字段（label/prob/归一化坐标），topk 不再适用，要换成框解析 + `eepimg` 画框画字。
- 如果你对「分类输出怎么来的」还想追根溯源，可回看 **u5-l2（输出读取与 epmat→ncnn::Mat 转换）** 的 `read_forward_result` 和 `epmat2nmat`，那是 `get_topk` 的数据源。
- 想理解裸机侧自实现的 `vector`/`pair` 为何能替代标准库，可预习 **u8-l3（simplestl 与 nmat：自实现容器与内存对齐）**。
- 建议阅读源码顺序：先 [Linux `classify/main.cpp`](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/classify/main.cpp)（结构最清晰），再 [裸机 `classify.cpp`](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/post_process/classify.cpp)，对照体会两者的同与不同。
