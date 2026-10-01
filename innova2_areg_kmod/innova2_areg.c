// SPDX-FileCopyrightText: 2026 the innova2 contributors
//
// SPDX-License-Identifier: GPL-2.0-only
/*
 * innova2_areg -- a supplemental module that re-creates the Innova2 tools chardev on kernels whose
 * mlx5 knows nothing about FPGAs.
 *
 * WHAT IT REPLACES
 *   `mlx5_fpga_tools` (drivers/net/ethernet/mellanox/mlx5/fpga/tools_char.c) shipped only in
 *   OFED 5.2-era packages.  OFED 23.10 and every DOCA-OFED (3.5.0 = mlnx-ofed-kernel 2607.x) still
 *   carry the in-kernel FPGA core -- mlx5_fpga_query(), mlx5_fpga_image_select(),
 *   mlx5_fpga_access_reg() are all still in .../mlx5/core/fpga/cmd.c -- but they DROP tools_char.c,
 *   so /dev/<bdf>_mlx5_fpga_tools never appears and every ioctl/lseek tool stops working.
 *
 * WHY THIS WORKS WITHOUT PATCHING ANY SHIPPING DRIVER
 *   Everything those ioctls did is one of two ConnectX access registers, and the register
 *   transport, mlx5_core_access_reg(), is EXPORT_SYMBOL_GPL in every version we checked (5.2,
 *   23.10, and upstream, where it is a thin wrapper over mlx5_access_reg()).  So this module needs
 *   no mlx5 FPGA support at all: it takes the struct mlx5_core_dev * that mlx5_core stashes with
 *   pci_set_drvdata() and issues the registers itself.
 *
 *     IOCTL_FPGA_QUERY      0x84  -> FPGA_CTRL       0x4023 read
 *     IOCTL_FPGA_IMAGE_SEL  0x83  -> FPGA_CTRL       0x4023 write, operation=FLASH_SELECT(3)
 *     IOCTL_FPGA_CONNECT    0x87  -> FPGA_CTRL       0x4023 read (query) / write op=9|0xA
 *     IOCTL_FPGA_CAP        0x85  -> FPGA_CAP        0x4022 read
 *     IOCTL_FPGA_TEMPERATURE 0x86 -> MTMP           0x900a read  (works on EVERY image)
 *     read()/write()+lseek        -> FPGA_ACCESS_REG 0x4024 {size, address, data}
 *
 *   The ioctl numbers, the by-value vs by-pointer argument convention and the big-endian 4-byte
 *   read/write on the node are all the ORIGINAL ones, so the vendor innova2_flex_app binary and our
 *   tools written for that node (innova2_app.py, the Flex repository's source/host scripts) run unmodified.
 *
 * WHY NOT USER SPACE
 *   Two user-space transports were measured first, and both fall short:
 *     * MFT/mlxreg through the PCI Vendor-Specific Capability (ICMD) gateway: FPGA_CAP and
 *       FPGA_CTRL *reads* work with no FPGA-aware driver at all, but every FPGA_CTRL *write*
 *       returns ME_ICMD_OPERATIONAL_ERROR while the identical write succeeds through the old ioctl
 *       on the same boot.  Reads yes, writes no.
 *     * rdma-core DEVX (mlx5dv_devx_general_cmd): ACCESS_REG (0x805) is not on the kernel's
 *       devx_is_general_cmd() whitelist, so it is rejected with EINVAL before it reaches firmware.
 *
 * SIZE DISCIPLINE.  The register lengths below are the ones the shipping driver sends, not the
 * minimum the layout needs -- in particular FPGA_ACCESS_REG is sent as
 * MLX5_ST_SZ_DW(fpga_access_reg) + MLX5_FPGA_ACCESS_REG_SIZE_MAX = 4 + 64 DWORDS = 272 bytes on
 * every call whatever the payload size.  Copy the driver; do not "fix" it.
 *
 * HAZARD.  image-select takes effect on the next COLD cycle and a JTAG grant detaches the
 * management path.  This module hands those back to user space exactly as the vendor driver did.
 */
#include <linux/module.h>
#include <linux/kernel.h>
#include <linux/pci.h>
#include <linux/cdev.h>
#include <linux/fs.h>
#include <linux/slab.h>
#include <linux/uaccess.h>
#include <linux/mutex.h>
#include <linux/version.h>
#include <asm/unaligned.h>

#define DRV "innova2_areg"

/* Declared locally rather than by including <linux/mlx5/driver.h>: this module is built against
 * whatever mlx5 the distro/OFED installed, and the only thing it needs from it is this symbol.
 * `struct mlx5_core_dev *` is opaque here -- the pointer is only ever passed straight back. */
extern int mlx5_core_access_reg(void *dev, void *data_in, int size_in,
				void *data_out, int size_out,
				u16 reg_id, int arg, int write);

#define REG_MTMP		0x900a	/* the ConnectX temperature register */
#define REG_FPGA_CAP		0x4022
#define REG_FPGA_CTRL		0x4023
#define REG_FPGA_ACCESS_REG	0x4024

#define FPGA_CTRL_SZ		16	/* MLX5_ST_SZ_BYTES(fpga_ctrl) */
#define FPGA_CAP_SZ		256	/* what the shipping mlxreg/driver reads */
#define ACCESS_REG_HDR		16	/* MLX5_ST_SZ_BYTES(fpga_access_reg) */
#define ACCESS_REG_MAX		64	/* MLX5_FPGA_ACCESS_REG_SIZE_MAX */
#define ACCESS_REG_SZ		(ACCESS_REG_HDR + ACCESS_REG_MAX * 4)	/* 272: what the driver sends */
#define MTMP_SZ			32	/* MLX5_ST_SZ_BYTES(mtmp_reg): 8 dwords, counted field by field */
#define MTMP_NAME_OFF		24	/* sensor_name[0x40] -- 8 bytes at byte 24, NOT 16 at byte 20 */
#define MTMP_NAME_LEN		8

/* THE SENSOR THAT WORKS ON EVERY IMAGE. mlx5_fpga_query_mtmp() reads the FPGA's thermal diode
 * through the ConnectX's OWN MTMP register at sensor index 63, NOT through CR space -- which is why
 * the vendor app can show a temperature while the User image runs and CR space is refused. The
 * ConnectX even names the sensor: MTMP.sensor_name reads "fpga_0". Anything that reads CR 0x8400
 * instead is reading the FPGA's on-die SYSMON, which only answers on Flex/Factory. */
#define MLX5_FPGA_SENSOR_DEVMON		63
#define MLX5_FPGA_INTERNAL_SENSORS_LOW	63
#define MLX5_FPGA_INTERNAL_SENSORS_HIGH	63

/* Original UAPI (include/uapi/linux/mlx5/fpga_tools.h) -- kept byte-for-byte compatible. */
#define IOCTL_ACCESS_TYPE	_IOW('m', 0x80, int)
#define IOCTL_FPGA_LOAD		_IOW('m', 0x81, int)
#define IOCTL_FPGA_RESET	 _IO('m', 0x82)
#define IOCTL_FPGA_IMAGE_SEL	_IOW('m', 0x83, int)
#define IOCTL_FPGA_QUERY	_IOR('m', 0x84, void *)
#define IOCTL_FPGA_CAP		_IOR('m', 0x85, void *)
#define IOCTL_FPGA_TEMPERATURE	_IOWR('m', 0x86, void *)
#define IOCTL_FPGA_CONNECT	_IOWR('m', 0x87, void *)

enum { OP_LOAD = 1, OP_RESET = 2, OP_FLASH_SELECT = 3, OP_DISCONNECT = 9, OP_CONNECT = 0xA };

/* struct mlx5_fpga_temperature, verbatim from the vendor app's tools_chardev.h -- field ORDER
 * matters, it is copied straight to user space. */
struct fpga_temperature {
	__u32	temperature;
	__u32	index;
	__u32	tee;
	__u32	max_temperature;
	__u32	temperature_threshold_hi;
	__u32	temperature_threshold_lo;
	__u32	mte;
	__u32	mtr;
	char	sensor_name[16];
};

struct areg_dev {
	struct list_head	list;
	struct pci_dev		*pdev;
	void			*mdev;		/* struct mlx5_core_dev * */
	struct cdev		cdev;
	struct device		*dev;
	dev_t			devt;
	struct mutex		lock;
	char			name[64];
};

static LIST_HEAD(areg_devs);
static struct class *areg_class;
static int areg_major;
static int areg_minor;

static inline u32 be_get(const u8 *p) { return get_unaligned_be32(p); }
static inline void be_put(u8 *p, u32 v) { put_unaligned_be32(v, p); }

static int ctrl_read(struct areg_dev *d, u32 out[4])
{
	u8 in[FPGA_CTRL_SZ] = {}, o[FPGA_CTRL_SZ] = {};
	int err, i;

	err = mlx5_core_access_reg(d->mdev, in, sizeof(in), o, sizeof(o),
				   REG_FPGA_CTRL, 0, 0);
	if (err)
		return err;
	for (i = 0; i < 4; i++)
		out[i] = be_get(o + 4 * i);
	return 0;
}

/* mlx5_fpga_ctrl_write(): ONLY operation (+ the image for FLASH_SELECT) is set; the register is not
 * read-modify-written.  Keeping that, because the firmware treats the other fields as reserved. */
static int ctrl_write(struct areg_dev *d, u8 op, bool have_img, u8 img)
{
	u8 in[FPGA_CTRL_SZ] = {}, o[FPGA_CTRL_SZ] = {};

	be_put(in + 0, (u32)op << 16);
	if (have_img)
		be_put(in + 4, (u32)img << 16);
	return mlx5_core_access_reg(d->mdev, in, sizeof(in), o, sizeof(o),
				    REG_FPGA_CTRL, 0, 1);
}

static int cr_access(struct areg_dev *d, u64 addr, void *buf, u8 size, bool write)
{
	u8 *in, *out;
	int err;

	if (size & 3 || (addr & 3) || size > ACCESS_REG_MAX)
		return -EINVAL;
	in = kzalloc(ACCESS_REG_SZ, GFP_KERNEL);
	out = kzalloc(ACCESS_REG_SZ, GFP_KERNEL);
	if (!in || !out) {
		kfree(in); kfree(out);
		return -ENOMEM;
	}
	be_put(in + 4, size);
	be_put(in + 8, (u32)(addr >> 32));
	be_put(in + 12, (u32)addr);
	if (write)
		memcpy(in + ACCESS_REG_HDR, buf, size);

	err = mlx5_core_access_reg(d->mdev, in, ACCESS_REG_SZ, out, ACCESS_REG_SZ,
				   REG_FPGA_ACCESS_REG, 0, write);
	if (!err && !write)
		memcpy(buf, out + ACCESS_REG_HDR, size);
	kfree(in); kfree(out);
	return err;
}

/* mlx5_fpga_query_mtmp(). The `i` bit marks a sensor OUTSIDE the internal range as an "external"
 * one; for index 63 (the FPGA diode) the driver clears it, so this does the same. */
static int mtmp_read(struct areg_dev *d, struct fpga_temperature *t)
{
	u8 in[MTMP_SZ] = {}, out[MTMP_SZ] = {};
	u32 idx = t->index;
	int err;

	be_put(in + 0, ((idx < MLX5_FPGA_INTERNAL_SENSORS_LOW ||
			 idx > MLX5_FPGA_INTERNAL_SENSORS_HIGH) ? 0x80000000u : 0u) |
			(idx & 0x7f));
	err = mlx5_core_access_reg(d->mdev, in, sizeof(in), out, sizeof(out),
				   REG_MTMP, 0, 0);
	if (err)
		return err;
	t->index			= be_get(out + 0) & 0x7f;
	t->temperature			= be_get(out + 4) & 0xffff;
	t->mte				= (be_get(out + 8) >> 31) & 1;
	t->mtr				= (be_get(out + 8) >> 30) & 1;
	t->max_temperature		= be_get(out + 8) & 0xffff;
	t->tee				= (be_get(out + 12) >> 30) & 3;
	t->temperature_threshold_hi	= be_get(out + 12) & 0xffff;
	t->temperature_threshold_lo	= be_get(out + 16) & 0xffff;
	/* sensor_name is 8 bytes in the register but 16 in the userspace struct: zero the field first
	 * so the extra half is not fed uninitialised kernel memory to user space. */
	memset(t->sensor_name, 0, sizeof(t->sensor_name));
	memcpy(t->sensor_name, out + MTMP_NAME_OFF, MTMP_NAME_LEN);
	return 0;
}

static int areg_open(struct inode *ip, struct file *fp)
{
	fp->private_data = container_of(ip->i_cdev, struct areg_dev, cdev);
	return 0;
}

/* The vendor node is byte-addressed: lseek to the CR address, then read/write. The vendor app only
 * ever moves 4 bytes at a time; larger transfers are chunked to the register's 64-byte maximum. */
static ssize_t areg_rw(struct file *fp, char __user *ubuf, size_t len, loff_t *off, bool write)
{
	struct areg_dev *d = fp->private_data;
	u8 chunk[ACCESS_REG_MAX];
	size_t done = 0;
	int err = 0;

	if (!len || (len & 3) || (*off & 3))
		return -EINVAL;
	if (mutex_lock_interruptible(&d->lock))
		return -ERESTARTSYS;
	while (done < len) {
		size_t n = min_t(size_t, len - done, ACCESS_REG_MAX);

		if (write && copy_from_user(chunk, ubuf + done, n)) { err = -EFAULT; break; }
		err = cr_access(d, *off + done, chunk, n, write);
		if (err) break;
		if (!write && copy_to_user(ubuf + done, chunk, n)) { err = -EFAULT; break; }
		done += n;
	}
	mutex_unlock(&d->lock);
	if (done) { *off += done; return done; }
	return err ? err : -EIO;
}

static ssize_t areg_read(struct file *f, char __user *b, size_t l, loff_t *o)
{ return areg_rw(f, b, l, o, false); }
static ssize_t areg_write(struct file *f, const char __user *b, size_t l, loff_t *o)
{ return areg_rw(f, (char __user *)b, l, o, true); }

static long areg_ioctl(struct file *fp, unsigned int cmd, unsigned long arg)
{
	struct areg_dev *d = fp->private_data;
	void __user *up = (void __user *)arg;
	u32 dw[4];
	int err;

	switch (cmd) {
	case IOCTL_FPGA_QUERY: {
		/* struct mlx5_fpga_query { enum admin_image; enum oper_image; enum image_status; }
		 * -- three C enums, 4 bytes each.  The original driver copies 12 bytes even though
		 * the _IOR size field says 8; matching that, because our fpga_query.py depends on
		 * the driver's actual write extent. */
		u32 q[3];

		if (mutex_lock_interruptible(&d->lock)) return -ERESTARTSYS;
		err = ctrl_read(d, dw);
		mutex_unlock(&d->lock);
		if (err) return err;
		q[0] = (dw[1] >> 16) & 0xff;	/* flash_select_admin */
		q[1] = dw[1] & 0xff;		/* flash_select_oper  */
		q[2] = dw[0] & 0xff;		/* status             */
		return copy_to_user(up, q, sizeof(q)) ? -EFAULT : 0;
	}
	case IOCTL_FPGA_CAP: {
		u8 *cap = kzalloc(FPGA_CAP_SZ, GFP_KERNEL);

		if (!cap) return -ENOMEM;
		if (mutex_lock_interruptible(&d->lock)) { kfree(cap); return -ERESTARTSYS; }
		err = mlx5_core_access_reg(d->mdev, cap, FPGA_CAP_SZ, cap, FPGA_CAP_SZ,
					   REG_FPGA_CAP, 0, 0);
		mutex_unlock(&d->lock);
		if (!err && copy_to_user(up, cap, FPGA_CAP_SZ)) err = -EFAULT;
		kfree(cap);
		return err;
	}
	case IOCTL_FPGA_TEMPERATURE: {
		struct fpga_temperature t;

		if (copy_from_user(&t, up, sizeof(t))) return -EFAULT;
		if (mutex_lock_interruptible(&d->lock)) return -ERESTARTSYS;
		err = mtmp_read(d, &t);
		mutex_unlock(&d->lock);
		if (err) return err;
		return copy_to_user(up, &t, sizeof(t)) ? -EFAULT : 0;
	}
	case IOCTL_FPGA_IMAGE_SEL:
		/* The vendor app passes the image BY VALUE despite the _IOW encoding. */
		if (arg > 3) return -EINVAL;
		if (mutex_lock_interruptible(&d->lock)) return -ERESTARTSYS;
		err = ctrl_write(d, OP_FLASH_SELECT, true, (u8)arg);
		mutex_unlock(&d->lock);
		return err;
	case IOCTL_FPGA_CONNECT: {
		int c;

		if (copy_from_user(&c, up, sizeof(c))) return -EFAULT;
		if (mutex_lock_interruptible(&d->lock)) return -ERESTARTSYS;
		if (c == 0) {				/* QUERY */
			err = ctrl_read(d, dw);
			if (!err)
				c = ((dw[0] & 0xff) == 3) ? OP_DISCONNECT : OP_CONNECT;
		} else if (c == OP_DISCONNECT || c == OP_CONNECT) {
			err = ctrl_write(d, (u8)c, false, 0);
		} else {
			err = -EINVAL;
		}
		mutex_unlock(&d->lock);
		if (err) return err;
		return copy_to_user(up, &c, sizeof(c)) ? -EFAULT : 0;
	}
	case IOCTL_FPGA_LOAD:
		if (arg > 3) return -EINVAL;
		if (mutex_lock_interruptible(&d->lock)) return -ERESTARTSYS;
		err = ctrl_write(d, OP_LOAD, true, (u8)arg);
		mutex_unlock(&d->lock);
		return err;
	case IOCTL_FPGA_RESET:
		if (mutex_lock_interruptible(&d->lock)) return -ERESTARTSYS;
		err = ctrl_write(d, OP_RESET, false, 0);
		mutex_unlock(&d->lock);
		return err;
	case IOCTL_ACCESS_TYPE:
		/* RDMA-vs-I2C access type: the old driver's "don't care" is all we implement, and
		 * access registers are the I2C path anyway.  Accepted so the vendor app's startup
		 * call does not fail. */
		return 0;
	default:
		return -ENOTTY;
	}
}

static const struct file_operations areg_fops = {
	.owner		= THIS_MODULE,
	.open		= areg_open,
	.read		= areg_read,
	.write		= areg_write,
	.llseek		= default_llseek,
	.unlocked_ioctl	= areg_ioctl,
};

static void areg_drop(struct areg_dev *d)
{
	if (d->dev) device_destroy(areg_class, d->devt);
	cdev_del(&d->cdev);
	pci_dev_put(d->pdev);
	list_del(&d->list);
	kfree(d);
}

static int areg_add(struct pci_dev *pdev)
{
	struct areg_dev *d;
	void *mdev = pci_get_drvdata(pdev);
	int err;

	/* pdev->driver was removed in 6.3; pdev->dev.driver has always been there and carries the
	 * same name, so bind the check to that instead of to a kernel version. */
	if (!mdev || !pdev->dev.driver || strcmp(pdev->dev.driver->name, "mlx5_core"))
		return 0;
	d = kzalloc(sizeof(*d), GFP_KERNEL);
	if (!d) return -ENOMEM;
	d->pdev = pci_dev_get(pdev);
	d->mdev = mdev;
	mutex_init(&d->lock);
	/* EXACTLY the vendor name, so unmodified tools find it. */
	snprintf(d->name, sizeof(d->name), "%04x:%02x:%02x.%d" "_mlx5_fpga_tools",
		 pci_domain_nr(pdev->bus), pdev->bus->number,
		 PCI_SLOT(pdev->devfn), PCI_FUNC(pdev->devfn));
	d->devt = MKDEV(areg_major, areg_minor++);
	cdev_init(&d->cdev, &areg_fops);
	d->cdev.owner = THIS_MODULE;
	err = cdev_add(&d->cdev, d->devt, 1);
	if (err) { pci_dev_put(pdev); kfree(d); return err; }
	d->dev = device_create(areg_class, &pdev->dev, d->devt, NULL, "%s", d->name);
	if (IS_ERR(d->dev)) {
		err = PTR_ERR(d->dev); d->dev = NULL;
		cdev_del(&d->cdev); pci_dev_put(pdev); kfree(d);
		return err;
	}
	list_add(&d->list, &areg_devs);
	pr_info(DRV ": /dev/%s\n", d->name);
	return 0;
}

static int __init areg_init(void)
{
	struct pci_dev *pdev = NULL;
	dev_t first;
	int err;

	err = alloc_chrdev_region(&first, 0, 16, DRV);
	if (err) return err;
	areg_major = MAJOR(first);
#if LINUX_VERSION_CODE >= KERNEL_VERSION(6, 4, 0)
	areg_class = class_create(DRV);
#else
	areg_class = class_create(THIS_MODULE, DRV);
#endif
	if (IS_ERR(areg_class)) {
		unregister_chrdev_region(first, 16);
		return PTR_ERR(areg_class);
	}
	while ((pdev = pci_get_device(PCI_VENDOR_ID_MELLANOX, PCI_ANY_ID, pdev)))
		areg_add(pdev);
	if (list_empty(&areg_devs))
		pr_warn(DRV ": no mlx5_core PCI function found -- nothing to attach to\n");
	return 0;
}

static void __exit areg_exit(void)
{
	struct areg_dev *d, *t;

	list_for_each_entry_safe(d, t, &areg_devs, list)
		areg_drop(d);
	class_destroy(areg_class);
	unregister_chrdev_region(MKDEV(areg_major, 0), 16);
}

module_init(areg_init);
module_exit(areg_exit);
MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("Innova2 tools chardev rebuilt on mlx5_core_access_reg");
