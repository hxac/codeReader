# 密码、加密与文档元数据

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚打开一份**加密文档**时的鉴权链路：先 `fz_needs_password` 把关、再 `fz_authenticatePassword` 验密码；
- 准确解释 `fz_authenticatePassword` 的**返回值是一个位掩码**（1=无需密码、2=用户密码、4=所有者密码），并据此判断「能不能继续渲染」「拥有哪些权限」；
- 用 `fz_lookup_metadata` 按键读取文档的格式、加密信息、标题、作者等元数据，并知道每个键的含义与「找不到时返回 -1」的约定。

本讲承接 [u3-l1 fz_document 与 fz_page 抽象](u3-l1-document-abstraction.md) 建立的「虚表 + 判空转发」模型：鉴权与元数据本质上就是 `fz_document` 虚表里的两组回调，通用层负责调度，真正干活的是各格式专用层（本讲以 PDF 为例深入到底）。

---

## 2. 前置知识

阅读本讲前，请先具备以下概念（若不熟悉，回到前置讲义复习）：

- **`fz_context`**：几乎所有 fitz 函数的第一个参数，是全局状态容器（见 [u2-l1](u2-l1-context.md)）。
- **`fz_document` 虚表**：`fz_document` 结构体里几乎全是函数指针，通用层函数（如 `fz_needs_password`）只做「指针非空就转发、为空就返回默认值」的调度，真正逻辑由格式专用层填写（见 [u3-l1](u3-l1-document-abstraction.md)）。
- **`fz_try` / `fz_catch`**：MuPDF 基于 `setjmp/longjmp` 的异常机制（见 [u2-l3](u2-l3-exceptions.md)）。鉴权失败通常**不抛异常**，而是用返回值表达，这是本讲的一个重点。

两个本讲要用到的 PDF 术语：

- **加密（Encryption）**：PDF 可以用口令加密，加密后字符串和流都被密钥加扰，必须拿到正确口令才能解出密钥、读出内容。
- **用户密码 / 所有者密码（User / Owner password）**：PDF 规范定义两种口令。用户密码用于「打开阅读」；所有者密码权限更高（通常拥有全部权限）。两者都可能为空。

---

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| `include/mupdf/fitz/document.h` | 通用层头文件：声明 `fz_needs_password`、`fz_authenticatePassword`、`fz_has_permission`、`fz_lookup_metadata`，定义 `fz_permission` 枚举与 `FZ_META_*` 键常量，并暴露 `struct fz_document` 的虚表字段。 |
| `source/fitz/document.c` | 通用层实现：上述四个函数的「判空 + 转发」包装。 |
| `source/pdf/pdf-crypt.c` | PDF 加密核心：`pdf_authenticate_password`、`pdf_needs_password`、`pdf_has_permission` 的真实算法。 |
| `include/mupdf/pdf/crypt.h` | PDF 权限位掩码 `PDF_PERM_*` 定义。 |
| `source/pdf/pdf-xref.c` | `pdf_lookup_metadata` 的真实实现，以及把 PDF 回调挂进虚表的 `pdf_new_document`。 |
| `source/tools/mudraw.c` | 工程级范例：渲染前如何把关密码（`-p` 选项）。 |
| `platform/gl/gl-main.c` | 工程级范例：如何用 `fz_lookup_metadata` / `fz_has_permission` 打印文档信息面板。 |

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**密码鉴权流程**、**鉴权返回值与权限**、**文档元数据读取**。

### 4.1 密码鉴权流程：needs_password → authenticate_password

#### 4.1.1 概念说明

并非所有文档都加密，也并非所有格式都支持加密。MuPDF 把「这份文档要不要密码」「这个密码对不对」拆成两个独立的问题，对应两个函数：

- `fz_needs_password(ctx, doc)`：返回非零表示「需要非空密码才能打开」，返回 0 表示「不需要密码」（要么没加密，要么空密码就够）。
- `fz_authenticatePassword(ctx, doc, password)`：拿一个候选密码去试，返回非零表示「通过」，返回 0 表示「失败」。

为什么拆两步？因为典型应用的交互流程是：先问「要不要弹密码框」，要弹才让用户输入、再拿输入去验。`mutool draw`、`mutool info` 都是这套套路。

#### 4.1.2 核心流程

标准的鉴权主循环伪代码：

```
doc = fz_open_document(ctx, filename)     // 仅读基本结构，不解密内容
if fz_needs_password(ctx, doc):           // 把关：是否需要密码
    if not fz_authenticatePassword(ctx, doc, pwd):   // 验密码
        报错 / 重新提示输入
// 到这里要么不需要密码、要么已通过，可以安全 count_pages / load_page / run_page
```

注意三个要点：

1. `fz_open_document` 本身**不验密码**——它能读出文档骨架（包括加密字典），所以即使密码错误也能成功打开；鉴权是后续独立的一步。
2. 鉴权失败**默认不抛异常**，靠返回值 0 表达，由调用者决定怎么办。
3. 通用层的这两个函数只是「转发器」，真正的判定逻辑在格式专用层。

#### 4.1.3 源码精读

先看通用层包装。`fz_needs_password` 只做「有回调就转发，没回调就返回 0（视为永远不需要密码）」：

[fz_needs_password：通用层判空转发](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/document.c#L689-L695)

```c
int
fz_needs_password(fz_context *ctx, fz_document *doc)
{
    if (doc && doc->needs_password)
        return doc->needs_password(ctx, doc);
    return 0;
}
```

`fz_authenticatePassword` 同理，但默认值是 `1`（视为永远通过）：

[fz_authenticatePassword：通用层判空转发](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/document.c#L697-L703)

```c
int
fz_authenticatePassword(fz_context *ctx, fz_document *doc, const char *password)
{
    if (doc && doc->authenticate_password)
        return doc->authenticate_password(ctx, doc, password);
    return 1;
}
```

这两个回调指针声明在 `struct fz_document` 的虚表里：

[struct fz_document 中的密码鉴权虚表字段](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/document.h#L1079-L1085)

```c
struct fz_document
{
    int refs;
    fz_document_drop_fn *drop_document;
    fz_document_needs_password_fn *needs_password;
    fz_document_authenticate_password_fn *authenticate_password;
    fz_document_has_permission_fn *has_permission;
    ...
```

真正干活的是 PDF 层。`pdf_needs_password` 的判定极其简洁——「没加密返回 0；空密码能通过也返回 0；否则返回 1」：

[pdf_needs_password：PDF 真实判定](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-crypt.c#L825-L833)

```c
int
pdf_needs_password(fz_context *ctx, pdf_document *doc)
{
    if (!doc->crypt)
        return 0;
    if (pdf_authenticate_password(ctx, doc, ""))
        return 0;
    return 1;
}
```

这里 `doc->crypt` 是 PDF 的加密上下文（解析 Encrypt 字典得到），为空表示文档未加密。`pdf_authenticate_password` 是密码校验的核心（见 4.2 节详解其返回值）。

最后看一个工程级用法。`mudraw` 在主循环里就是教科书般的「两步把关」，密码来自命令行 `-p`：

[mudraw：渲染前的密码把关](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mudraw.c#L2700-L2705)

```c
if (fz_needs_password(ctx, doc))
{
    if (!fz_authenticate_password(ctx, doc, password))
        fz_throw(ctx, FZ_ERROR_ARGUMENT, "cannot authenticate password: %s", filename);
}
```

`-p` 选项的接线点在主函数选项解析处（默认空字符串）：

[mudraw：-p 选项默认值与解析](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mudraw.c#L2135-L2157)

#### 4.1.4 代码实践

**实践目标**：亲手观察「两步鉴权」的真实返回值。

**操作步骤**（命令行路径，无需写 C）：

1. 准备一份加密 PDF（记其密码为 `SECRET`）。若手头没有，可用外部工具生成，例如 `qpdf --encrypt "" SECRET 256 -- input.pdf enc.pdf`（`qpdf` 不是 MuPDF 的一部分，仅用于造测试样本）。
2. 分别用错误密码与正确密码调用 `mutool`：

```bash
./build/release/mutool draw -o /dev/null -p WRONGPASS enc.pdf 1
./build/release/mutool draw -o /dev/null -p SECRET    enc.pdf 1
```

**需要观察的现象**：

- 错误密码时，`mutool` 报 `cannot authenticate password: enc.pdf` 并退出（对应上面 `fz_throw` 那一行）。
- 正确密码时，正常渲染第 1 页。

**预期结果**：鉴权失败由返回值 0 触发报错，而非崩溃；鉴权通过后才进入渲染。

**待本地验证**：如果你生成的样本加密强度/版本不同（V4/R4 vs V5/R6），行为一致，但耗时可能不同。

#### 4.1.5 小练习与答案

**练习 1**：如果一个格式（比如某图片格式）的 handler 没有实现 `needs_password` 回调，调用 `fz_needs_password` 会怎样？

**参考答案**：通用层走 `if (doc && doc->needs_password)` 判空，指针为 NULL 时直接返回 0，即「视为永远不需要密码」。同理 `authenticate_password` 为 NULL 时返回 1（视为永远通过）。这正是「判空 + 转发」模型让不支持的格式自动获得合理默认行为的好处。

**练习 2**：为什么 `fz_open_document` 成功返回，不代表密码已经通过？

**参考答案**：因为打开阶段只读文档基本结构（PDF 里就是 xref 与 Encrypt 字典），这些结构本身并不被内容密钥加扰。解密内容流的密钥要在 `authenticate_password` 里才算出来并存进 `doc->crypt`，所以「能打开」与「能解密」是两件事。

---

### 4.2 鉴权返回值与权限

#### 4.2.1 概念说明

`fz_authenticatePassword` 的返回值**不是简单的 true/false**，而是一个位掩码。头文件的文档注释把含义写得很清楚：

- Bit 0（值 1）=> 无需密码（文档未加密）
- Bit 1（值 2）=> 用户密码通过
- Bit 2（值 4）=> 所有者密码通过

之所以设计成位掩码，是因为同一份文档可能「用户密码 = 所有者密码」，这时两个位会同时置位。返回值还顺带携带「你以什么身份打开的」信息，这直接决定了**权限**：用所有者密码打开通常拥有全部权限，用用户密码打开则受文档权限位 `P` 限制。

权限通过 `fz_has_permission(ctx, doc, p)` 查询，`p` 取自 `fz_permission` 枚举（打印、复制、编辑、批注、表单、无障碍、组装、高质量打印）。

#### 4.2.2 核心流程

PDF 鉴权的返回值是这样算出来的（在 `pdf_authenticate_password` 内）：

```
若未加密              -> 返回 1            (bit0)
否则 access = 0
  若 user_pw 命中     -> access |= 2       (bit1)
  若 owner_pw 命中    -> access |= 4       (bit2)
返回 access
```

其中 `user_pw` / `owner_pw` 的「命中」是把候选密码按 PDF 规范做哈希（MD5/RC4 或 R5/R6 的 SHA-256 派生），再与文档里存的 `U` / `O` 值比较。

权限判定则是一个按位与。设文档权限字为 \(P\)，某个权限对应第 \(k\) 位，则：

\[ \text{granted}(k) \iff (P\ \&\ (1 \ll k)) \ne 0 \]

各权限对应的位（来自 PDF 规范、定义在 `crypt.h`）：打印=2、修改=3、复制=4、批注=5、表单=8、无障碍=9、组装=10、高质量打印=11。若以所有者密码打开（`access & 4`），所有权限一律放行。

#### 4.2.3 源码精读

头文件对返回值的权威说明（这是理解整个鉴权的钥匙）：

[fz_authenticatePassword 返回值的位含义](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/document.h#L635-L651)

PDF 层 `pdf_authenticate_password` 的核心——先归零 `access`，再分别试用户/所有者密码并置位：

[pdf_authenticate_password：设置 access 位掩码](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-crypt.c#L803-L822)

```c
doc->crypt->access = 0;
if (pdf_authenticate_user_password(ctx, doc->crypt, (unsigned char *)password, strlen(password)))
    doc->crypt->access = 2;
if (pdf_authenticate_owner_password(ctx, doc->crypt, (unsigned char *)password, strlen(password)))
    doc->crypt->access |= 4;
else if (doc->crypt->access & 2)
{
    /* 失败的 owner 验证会破坏已存的密钥，需重新跑一次 user 验证 */
    (void)pdf_authenticate_user_password(ctx, doc->crypt, (unsigned char *)password, strlen(password));
}
/* 为与 Acrobat 一致：仅空 owner 命中时不认 */
if (*password == 0 && doc->crypt->access == 4)
    doc->crypt->access = 0;
return doc->crypt->access;
```

`access` 字段的含义在结构体里有注释——值 2 表示用户、4 表示所有者、6 表示两者皆中：

[pdf_crypt 结构体的 access 字段注释](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-crypt.c#L59-L63)

用户密码的「命中」本质是比较哈希输出与文档里的 `U` 值（按加密版本 R2/R3-R4/R5/R6 比较不同长度）：

[pdf_authenticate_user_password：哈希比对](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-crypt.c#L657-L667)

再看权限。`fz_has_permission` 在通用层依然是判空转发，默认放行（返回 1）：

[fz_has_permission：通用层转发](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/document.c#L705-L711)

PDF 层的权限判定：未加密或以所有者身份打开则全放行，否则按位与查 `P` 字：

[pdf_has_permission：按权限位判定](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-crypt.c#L835-L854)

```c
if (!doc->crypt)
    return 1;
if (doc->crypt->access & 4) /* unlocked with owner password */
    return 1;
switch (p)
{
case FZ_PERMISSION_PRINT: return doc->crypt->p & PDF_PERM_PRINT;
case FZ_PERMISSION_EDIT:  return doc->crypt->p & PDF_PERM_MODIFY;
case FZ_PERMISSION_COPY:  return doc->crypt->p & PDF_PERM_COPY;
...
}
```

权限位掩码定义在 `crypt.h`（注意位号与 PDF 规范一致）：

[PDF_PERM_*：权限位定义](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/pdf/crypt.h#L74-L84)

```c
PDF_PERM_PRINT = 1 << 2,
PDF_PERM_MODIFY = 1 << 3,
PDF_PERM_COPY = 1 << 4,
PDF_PERM_ANNOTATE = 1 << 5,
PDF_PERM_FORM = 1 << 8,
PDF_PERM_ACCESSIBILITY = 1 << 9, /* pdf 2.0 起恒为授予 */
PDF_PERM_ASSEMBLE = 1 << 10,
PDF_PERM_PRINT_HQ = 1 << 11,
```

而通用层的 `fz_permission` 枚举用字符常量命名同一组权限，供 `fz_has_permission` 的调用方使用：

[fz_permission 枚举](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/document.h#L119-L130)

#### 4.2.4 代码实践

**实践目标**：亲眼看到「错误密码返回 0、正确密码返回 2 或 4」。

**操作步骤**（示例代码，非项目原有代码）：在 `docs/examples/` 下新建一个 `pwtest.c`，复用 `example.c` 的 context 打开骨架，加入鉴权探针。

```c
/* 示例代码：仅用于演示鉴权返回值，非项目原有文件 */
#include "mupdf/fitz.h"
#include <stdio.h>

int main(int argc, char **argv)
{
    fz_context *ctx = fz_new_context(NULL, NULL, FZ_STORE_DEFAULT);
    fz_register_document_handlers(ctx);

    fz_try(ctx)
    {
        fz_document *doc = fz_open_document(ctx, argv[1]);
        printf("needs_password = %d\n", fz_needs_password(ctx, doc));

        const char *pws[] = { "WRONG", argv[2] /* 正确密码 */ };
        for (int i = 0; i < 2; i++)
            printf("authenticate %-8s -> access=%d (bit0=%d bit1=%d bit2=%d)\n",
                pws[i],
                fz_authenticatePassword(ctx, doc, pws[i]),
                fz_authenticatePassword(ctx, doc, pws[i]) & 1,
                fz_authenticatePassword(ctx, doc, pws[i]) & 2,
                fz_authenticatePassword(ctx, doc, pws[i]) & 4);

        /* 用正确密码再次鉴权后查权限 */
        fz_authenticatePassword(ctx, doc, argv[2]);
        printf("can copy? %d  can print? %d\n",
            fz_has_permission(ctx, doc, FZ_PERMISSION_COPY),
            fz_has_permission(ctx, doc, FZ_PERMISSION_PRINT));

        fz_drop_document(ctx, doc);
    }
    fz_catch(ctx)
        fprintf(stderr, "error: %s\n", fz_caught_message(ctx));

    fz_drop_context(ctx);
    return 0;
}
```

编译可仿照 `example.c` 的链接方式（链接 `libmupdf` 与 `libmupdf-third`）。

**需要观察的现象**：

- `needs_password` 对加密文档输出 1，对普通文档输出 0。
- 错误密码：`access=0`（三个 bit 全 0）。
- 正确密码：若你是用用户密码打开，`access=2`（bit1=1）；若文档用户密码=所有者密码，可能是 `access=6`。

**预期结果**：返回值确实是位掩码，且 `fz_has_permission` 的结果随鉴权身份变化。

**待本地验证**：不同加密样本（仅设了所有者密码、用户密码=所有者密码等）下 `access` 的具体数值会不同。

#### 4.2.5 小练习与答案

**练习 1**：代码里常见写法是 `if (!fz_authenticatePassword(ctx, doc, pwd)) 报错;`。为什么这里可以把位掩码当布尔用？

**参考答案**：因为「失败」时返回值恰好是 0（`access` 归零、没有任何位被置位），而任何成功情形（1/2/4/6）都非零。C 的 `!` 把 0 当真、非零当假，所以「失败即 `!ret` 为真」恰好等价于「未通过鉴权」。

**练习 2**：用所有者密码打开的文档，`fz_has_permission(ctx, doc, FZ_PERMISSION_COPY)` 一定返回 1 吗？为什么？

**参考答案**：是的。`pdf_has_permission` 在 `doc->crypt->access & 4`（所有者身份）时直接 `return 1`，根本不再看权限位 `P`。所有者密码语义上就是「全权」。

---

### 4.3 文档元数据读取：fz_lookup_metadata

#### 4.3.1 概念说明

除了鉴权，`fz_document` 虚表还提供 `lookup_metadata` 回调，用于按键读取文档的「元数据」字符串：格式与版本、加密描述、标题、作者、主题、关键词、创建/修改时间等。这套 API 是格式无关的——同一份调用代码既能读 PDF 的 Info 字典，也能读 EPUB 的 metadata。

`fz_lookup_metadata(ctx, doc, key, buf, size)` 的关键约定：

- `buf` 是调用方提供的缓冲区，`size` 是其容量；
- 返回值是「**存下结果字符串（含结尾 `\0`）所需的总字节数**」，可能大于 `size`（说明被截断了）；
- 若键不被识别或文档里没有该字段，返回 **-1**；
- 返回前会把 `buf[0]` 置 0（即使失败也是空串），避免调用方读到野内容。

所以判断「有没有取到」的标准写法是 `ret > 0`（注意不是 `>= 0`，也不是 `!= -1`，因为截断时返回值会大于 size 但仍为正）。

#### 4.3.2 核心流程

调用流程：

```
ret = fz_lookup_metadata(ctx, doc, key, buf, sizeof buf)
if ret > 0:        使用 buf 里的字符串（注意可能被截断）
else /* ret == -1 */: 该键不支持或文档无此字段
```

合法的 `key` 分两类（定义在 `document.h`）：

- 基本信息：`"format"`（格式与版本，如 `PDF 1.7`）、`"encryption"`（加密描述，如 `Standard V5 R6 256-bit AES`，未加密为 `None`）。
- 文档信息字典（前缀 `info:`）：`info:Title`、`info:Author`、`info:Subject`、`info:Keywords`、`info:Creator`、`info:Producer`、`info:CreationDate`、`info:ModDate`。

#### 4.3.3 源码精读

通用层包装，注意它进入回调前先把 `buf[0]` 置 0、无回调时返回 -1：

[fz_lookup_metadata：通用层转发并预清空缓冲区](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/document.c#L953-L961)

```c
int
fz_lookup_metadata(fz_context *ctx, fz_document *doc, const char *key, char *buf, size_t size)
{
    if (buf && size > 0)
        buf[0] = 0;
    if (doc && doc->lookup_metadata)
        return doc->lookup_metadata(ctx, doc, key, buf, size);
    return -1;
}
```

键常量与函数文档（权威的 key 清单）：

[FZ_META_* 键常量与 fz_lookup_metadata 文档](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/document.h#L942-L985)

PDF 层 `pdf_lookup_metadata` 按 key 分派：`format` 取 PDF 版本号、`encryption` 拼加密参数、`info:Xxx` 去 trailer 的 Info 字典里查对应名字：

[pdf_lookup_metadata：按 key 分派](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-xref.c#L3026-L3082)

关键片段：

```c
if (!strcmp(key, FZ_META_FORMAT))
    return 1 + (int)fz_snprintf(buf, size, "PDF %d.%d", version/10, version % 10);

if (!strcmp(key, FZ_META_ENCRYPTION))
    if (doc->crypt) { /* 拼 "Standard V%d R%d %d-bit ..." */ }
    else
        return 1 + (int)fz_strlcpy(buf, "None", size);

if (strstr(key, "info:") == key)
{
    info = pdf_dict_get(ctx, pdf_trailer(ctx, doc), PDF_NAME(Info));
    if (!info) return -1;
    info = pdf_dict_gets(ctx, info, key + 5);   /* 跳过 "info:" 前缀 */
    if (!info) return -1;
    s = pdf_to_text_string(ctx, info);
    if (strlen(s) <= 0) return -1;
    return 1 + (int)fz_strlcpy(buf, s, size);
}
return -1;
```

注意 `1 + fz_snprintf/strlcpy`：因为这两个函数返回「不含结尾 `\0` 的字节数」，加 1 正好符合「含 `\0` 的总长度」这一返回值约定。

这套回调是怎么挂进虚表的？在 `pdf_new_document` 里逐字段赋值：

[pdf_new_document：把 PDF 回调挂进虚表](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-xref.c#L3220-L3232)

```c
doc->super.needs_password = pdf_needs_password_imp;
doc->super.authenticate_password = pdf_authenticate_password_imp;
doc->super.has_permission = pdf_has_permission_imp;
...
doc->super.lookup_metadata = pdf_lookup_metadata_imp;
doc->super.set_metadata = pdf_set_metadata_imp;
```

最后看一个工程级用法——`mupdf-gl` 的信息面板逐个键打印，并用 `fz_has_permission` 列出权限，是「元数据 + 权限」组合的最佳范本：

[gl-main.c：逐键打印元数据](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/platform/gl/gl-main.c#L2651-L2685)

```c
if (fz_lookup_metadata(ctx, doc, FZ_META_INFO_TITLE, buf, sizeof buf) > 0)
    fz_append_printf(ctx, out, "Title: %s\n", buf);
if (fz_lookup_metadata(ctx, doc, FZ_META_FORMAT, buf, sizeof buf) > 0)
    fz_append_printf(ctx, out, "Format: %s\n", buf);
if (fz_lookup_metadata(ctx, doc, FZ_META_ENCRYPTION, buf, sizeof buf) > 0)
    fz_append_printf(ctx, out, "Encryption: %s\n", buf);
```

注意它统一用 `> 0` 判定——这正是 4.3.1 节强调的正确写法。

#### 4.3.4 代码实践

**实践目标**：复刻 `gl-main.c` 的信息面板，把一份文档的元数据完整打印出来。

**操作步骤**：

1. 用 `mutool info`（内部走 `source/tools/pdfinfo.c`）快速观察一份 PDF 的元数据：

```bash
./build/release/mutool info your.pdf
```

2. （示例代码）仿照 `gl-main.c` 写一个小工具 `metainfo.c`，对一个文档把 `format`、`encryption`、`info:Title`、`info:Author`、`info:Creator`、`info:Producer` 逐个打印，找不到的键跳过。

**需要观察的现象**：

- 加密文档的 `encryption` 键会输出类似 `Standard V5 R6 256-bit AES`；普通文档输出 `None`。
- `format` 对 PDF 输出 `PDF 1.7` 之类；对 EPUB/XPS 输出对应字符串（说明同一套 API 跨格式生效）。
- 没有 Info 字典的文档，所有 `info:*` 键都返回 -1、不打印。

**预期结果**：`> 0` 判定的键被打印，其余被静默跳过；`buf` 永远是干净的空串或合法字符串。

**待本地验证**：不同文档实际能取到的键差异较大，以你本地样本为准。

#### 4.3.5 小练习与答案

**练习 1**：为什么判断「取到了元数据」要用 `ret > 0` 而不是 `ret >= 0` 或 `ret != -1`？

**参考答案**：返回值的语义是「含结尾 `\0` 的总长度」。成功时至少为 1（哪怕空串也含一个 `\0`，但 PDF 层对空串会返回 -1，所以实际成功值 ≥ 1）；失败时为 -1。`>= 0` 会把不存在的 -1 排除但理论上把 0 当成功（实际不会出现 0），而 `> 0` 最精确地表达「确实写入了内容」。

**练习 2**：`info:Title` 这个键在 PDF 层最终是怎么取到值的？

**参考答案**：`pdf_lookup_metadata` 发现 key 以 `"info:"` 开头，于是从 `pdf_trailer(doc)` 取出 `Info` 字典（PDF 的文档信息字典），再用 `key + 5`（即去掉 `info:` 前缀后的 `Title`）在 Info 字典里 `pdf_dict_gets`，最后用 `pdf_to_text_string` 把取到的字符串对象转成 C 字符串。

---

## 5. 综合实践

把本讲三个模块串起来，写一个迷你的 **`safeview` 文档检视工具**：它接受一个文档路径和可选密码，依次完成：

1. 创建 context、注册 handler、打开文档（包在 `fz_try/fz_catch` 里）。
2. 用 `fz_needs_password` 判断是否加密；若加密，用命令行传入的密码 `fz_authenticatePassword`，失败则打印「鉴权失败 access=0」并退出。
3. 鉴权通过后，打印 `format`、`encryption` 两个元数据键，以及 `access` 值拆出的三个 bit 含义（无需密码 / 用户密码 / 所有者密码）。
4. 用 `fz_has_permission` 列出 copy/print/edit 三个权限是否 granted。
5. 最后 `fz_count_pages` 打印总页数，并逆序释放：`fz_drop_document` → `fz_drop_context`。

验收要点：

- 对普通文档（无密码）：`needs_password=0`、`encryption=None`、权限全 granted。
- 对加密文档 + 正确密码：`needs_password=1`、`encryption` 显示加密参数、权限取决于文档 `P` 字与你的身份。
- 对加密文档 + 错误密码：在第 2 步即退出，不会走到第 3 步。

这个练习同时覆盖了「鉴权流程」「返回值含义」「元数据读取」三个模块，并复用了 [u3-l1](u3-l1-document-abstraction.md) 的资源释放顺序铁律（page→document→context）。

---

## 6. 本讲小结

- 鉴权是两步：`fz_needs_password` 把关「要不要密码」，`fz_authenticatePassword` 验「密码对不对」；`fz_open_document` 本身不验密码。
- 通用层的鉴权/元数据函数都是「判空 + 转发」：回调为 NULL 时给出合理默认（不加密格式视为不需要密码、鉴权恒通过、元数据返回 -1）。
- `fz_authenticatePassword` 返回值是位掩码：1=无需密码、2=用户密码、4=所有者密码；`if (!ret)` 当布尔用之所以成立，是因为失败恰好是 0。
- 权限用 `fz_has_permission` + `fz_permission` 枚举查询；以所有者身份打开时一律放行，否则按 PDF 权限字 `P` 的位（`PDF_PERM_*`）判定。
- `fz_lookup_metadata` 按键取值，返回「含 `\0` 的总长度」、找不到返回 -1、进入前预清空 `buf`；判定成功用 `> 0`。键分两类：`format`/`encryption` 与 `info:*`。
- PDF 的真实逻辑全在 `pdf-crypt.c`（鉴权算法）与 `pdf-xref.c`（元数据分派、虚表挂接），工程级用法可参考 `mudraw.c` 与 `platform/gl/gl-main.c`。

---

## 7. 下一步学习建议

- 想深入 PDF 加密算法本身（MD5/RC4/SHA-256 派生、R2~R6 各版本差异），精读 `source/pdf/pdf-crypt.c` 全文，它是 PDF 1.7 「Algorithm 3.x」系列的可运行实现。
- 想了解 PDF 对象模型（`Info` 字典、`trailer`、`pdf_obj` 七种类型），进入 [u7-l1 pdf_obj：PDF 对象类型系统](u7-l1-pdf-object-model.md)，本讲的 `pdf_dict_gets(ctx, info, key+5)` 就是对象模型的直接应用。
- 想继续「文档抽象」单元，可以接 [u4-l1 fz_device：显示设备抽象](u4-l1-device-model.md)，把鉴权通过后的页面真正渲染出来。
- 若关心写入侧（设置元数据、改密码、重写加密），看 `fz_set_metadata`（`source/fitz/document.c`）与 `source/pdf/pdf-write.c`，对应大纲中的 u6（导出与转换）与 u7-l3（解析与写入）。
