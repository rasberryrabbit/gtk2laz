#!/usr/bin/env python3
"""
gen_fake_includes.py
====================
Generate a complete set of fake libc / GTK stub headers for pycparser.

These stubs declare just enough types and function prototypes that
pycparser's `parse_file(..., use_cpp=True)` succeeds on a typical
GTK application without dragging in the real system headers (which
would blow up the AST with thousands of internal symbols).
"""
import os
import sys

ROOT = sys.argv[1] if len(sys.argv) > 1 else "/tmp/fake_libc_include"

HEADERS = {
    "stdint.h": """#ifndef _STDINT_H
#define _STDINT_H
typedef int int8_t;
typedef int int16_t;
typedef int int32_t;
typedef long long int64_t;
typedef unsigned int uint8_t;
typedef unsigned int uint16_t;
typedef unsigned int uint32_t;
typedef unsigned long long uint64_t;
typedef unsigned long uintptr_t;
typedef long intptr_t;
#endif
""",
    "stdio.h": """#ifndef _STDIO_H
#define _STDIO_H
typedef struct _IO_FILE FILE;
extern int printf(const char *, ...);
extern int fprintf(FILE *, const char *, ...);
extern int sprintf(char *, const char *, ...);
extern int snprintf(char *, unsigned long, const char *, ...);
extern FILE *fopen(const char *, const char *);
extern int fclose(FILE *);
extern unsigned long fread(void *, unsigned long, unsigned long, FILE *);
extern unsigned long fwrite(const void *, unsigned long, unsigned long, FILE *);
extern int fseek(FILE *, long, int);
extern long ftell(FILE *);
extern int feof(FILE *);
extern char *fgets(char *, int, FILE *);
extern int fputs(const char *, FILE *);
extern int fgetc(FILE *);
extern int fputc(int, FILE *);
#endif
""",
    "stdlib.h": """#ifndef _STDLIB_H
#define _STDLIB_H
typedef unsigned long size_t;
extern void *malloc(size_t);
extern void *calloc(size_t, size_t);
extern void *realloc(void *, size_t);
extern void free(void *);
extern void exit(int);
extern int atoi(const char *);
extern long strtol(const char *, char **, int);
extern double atof(const char *);
#endif
""",
    "string.h": """#ifndef _STRING_H
#define _STRING_H
typedef unsigned long size_t;
extern char *strcpy(char *, const char *);
extern char *strncpy(char *, const char *, size_t);
extern char *strcat(char *, const char *);
extern char *strncat(char *, const char *, size_t);
extern int strcmp(const char *, const char *);
extern int strncmp(const char *, const char *, size_t);
extern int strcasecmp(const char *, const char *);
extern int strncasecmp(const char *, const char *, size_t);
extern size_t strlen(const char *);
extern char *strchr(const char *, int);
extern char *strrchr(const char *, int);
extern char *strstr(const char *, const char *);
extern char *strdup(const char *);
extern void *memcpy(void *, const void *, size_t);
extern void *memmove(void *, const void *, size_t);
extern void *memset(void *, int, size_t);
extern int memcmp(const void *, const void *, size_t);
#endif
""",
    "unistd.h": """#ifndef _UNISTD_H
#define _UNISTD_H
extern int close(int);
extern long read(int, void *, unsigned long);
extern long write(int, const void *, unsigned long);
extern int access(const char *, int);
extern int unlink(const char *);
extern unsigned int sleep(unsigned int);
extern int usleep(unsigned int);
#endif
""",
    "time.h": """#ifndef _TIME_H
#define _TIME_H
typedef long time_t;
struct tm { int tm_sec; int tm_min; int tm_hour; int tm_mday; int tm_mon; int tm_year; };
extern time_t time(time_t *);
extern struct tm *localtime(const time_t *);
#endif
""",
    "ctype.h": """#ifndef _CTYPE_H
#define _CTYPE_H
extern int isdigit(int);
extern int isalpha(int);
extern int isalnum(int);
extern int isspace(int);
extern int isupper(int);
extern int islower(int);
extern int tolower(int);
extern int toupper(int);
#endif
""",
    "getopt.h": """#ifndef _GETOPT_H
#define _GETOPT_H
struct option { const char *name; int has_arg; int *flag; int val; };
extern int getopt_long(int, char *const *, const char *, const struct option *, int *);
extern int getopt(int, char *const *, const char *);
#endif
""",
    "fcntl.h": """#ifndef _FCNTL_H
#define _FCNTL_H
extern int open(const char *, int, ...);
#define O_RDONLY 0
#define O_WRONLY 1
#define O_RDWR 2
#define O_CREAT 0x40
#define O_TRUNC 0x80
#define O_APPEND 0x400
#endif
""",
    "errno.h": """#ifndef _ERRNO_H
#define _ERRNO_H
extern int errno;
#define EAGAIN 11
#define EBADF 9
#define EINVAL 22
#define ENOENT 2
#define ENODATA 61
#endif
""",
    "wchar.h": """#ifndef _WCHAR_H
#define _WCHAR_H
typedef int wchar_t;
typedef unsigned long wint_t;
#endif
""",
    "math.h": """#ifndef _MATH_H
#define _MATH_H
extern double sqrt(double);
extern double pow(double, double);
extern double floor(double);
extern double ceil(double);
extern double round(double);
#endif
""",
    "sys/types.h": """#ifndef _SYS_TYPES_H
#define _SYS_TYPES_H
typedef long ssize_t;
typedef unsigned long size_t;
typedef int pid_t;
typedef int uid_t;
typedef int gid_t;
typedef int mode_t;
typedef long off_t;
typedef long time_t;
typedef unsigned long uintptr_t;
typedef long intptr_t;
#endif
""",
    "sys/stat.h": """#ifndef _SYS_STAT_H
#define _SYS_STAT_H
struct stat { long st_size; int st_mode; long st_mtime; };
extern int stat(const char *, struct stat *);
extern int fstat(int, struct stat *);
#define S_IFMT 0xF000
#define S_IFDIR 0x4000
#define S_IFREG 0x8000
#endif
""",
    "sys/ioctl.h": """#ifndef _SYS_IOCTL_H
#define _SYS_IOCTL_H
extern int ioctl(int, unsigned long, ...);
#endif
""",
    "sys/timeb.h": """#ifndef _SYS_TIMEB_H
#define _SYS_TIMEB_H
struct timeb { long time; unsigned short millitm; short timezone; short dstflag; };
extern int ftime(struct timeb *);
#endif
""",
    "linux/hiddev.h": """#ifndef _LINUX_HIDDEV_H
#define _LINUX_HIDDEV_H
struct hiddev_devinfo { int bustype; int busnum; int devnum; int num_applications; };
struct hiddev_collection_info { int type; int usage; int level; };
struct hiddev_report_info { int report_type; int report_id; int num_fields; };
struct hiddev_field_info { int report_type; int report_id; int field_index; int maxusage; };
struct hiddev_usage_ref { int report_type; int report_id; int field_index; int usage_index; int usage_code; int value; };
struct hiddev_usage_ref_multi { struct hiddev_usage_ref uref; int num_values; int values[1024]; };
#define HIDIOCGDEVINFO 0x48004001
#define HIDIOCGCOLLECTIONINFO 0x48004002
#define HIDIOCGREPORT 0x48004003
#define HIDIOCGREPORTINFO 0x48004004
#define HIDIOCGFIELDINFO 0x48004005
#define HIDIOCGUSAGE 0x48004006
#define HIDIOCSUSAGE 0x48004007
#endif
""",
    "linux/hidraw.h": """#ifndef _LINUX_HIDRAW_H
#define _LINUX_HIDRAW_H
#define HIDRAW_FIRST_MINOR 0
#define HIDRAW_MAX_DEVICES 64
#define HIDIOCGRDESCSIZE 0x48044001
#define HIDIOCGRDESC 0x90044802
#define HIDIOCGRAWINFO 0x80084803
#define HIDIOCGRAWNAME(len) 0x81044804
#endif
""",
    "linux/input.h": """#ifndef _LINUX_INPUT_H
#define _LINUX_INPUT_H
struct input_event { long tv_sec; long tv_usec; unsigned short type; unsigned short code; int value; };
#endif
""",
    "asm/types.h": """#ifndef _ASM_TYPES_H
#define _ASM_TYPES_H
typedef unsigned int __u32;
typedef unsigned short __u16;
typedef unsigned char __u8;
typedef int __s32;
typedef short __s16;
typedef signed char __s8;
#endif
""",
    "gtk/gtk.h": """#ifndef __GTK_H__
#define __GTK_H__
typedef struct _GtkWidget GtkWidget;
typedef struct _GtkWindow GtkWindow;
typedef struct _GtkButton GtkButton;
typedef struct _GtkLabel GtkLabel;
typedef struct _GtkEntry GtkEntry;
typedef struct _GtkBox GtkBox;
typedef struct _GtkNotebook GtkNotebook;
typedef struct _GtkComboBox GtkComboBox;
typedef struct _GtkComboBoxText GtkComboBoxText;
typedef struct _GtkSpinButton GtkSpinButton;
typedef struct _GtkTextView GtkTextView;
typedef struct _GtkTextBuffer GtkTextBuffer;
typedef struct _GtkTextIter GtkTextIter;
typedef struct _GtkStatusbar GtkStatusbar;
typedef struct _GtkTreeView GtkTreeView;
typedef struct _GtkTreeSelection GtkTreeSelection;
typedef struct _GtkListStore GtkListStore;
typedef struct _GtkTreeIter GtkTreeIter;
typedef struct _GtkToggleToolButton GtkToggleToolButton;
typedef struct _GtkToggleButton GtkToggleButton;
typedef struct _GtkCheckButton GtkCheckButton;
typedef struct _GtkRadioButton GtkRadioButton;
typedef struct _GtkToolButton GtkToolButton;
typedef struct _GtkToolbar GtkToolbar;
typedef struct _GtkMenuBar GtkMenuBar;
typedef struct _GtkMenu GtkMenu;
typedef struct _GtkMenuItem GtkMenuItem;
typedef struct _GtkCheckMenuItem GtkCheckMenuItem;
typedef struct _GtkImage GtkImage;
typedef struct _GtkAdjustment GtkAdjustment;
typedef struct _GtkBuilder GtkBuilder;
typedef struct _GdkPixbuf GdkPixbuf;
typedef struct _GdkScreen GdkScreen;
typedef struct _GtkCssProvider GtkCssProvider;
typedef int gboolean;
typedef int gint;
typedef unsigned int guint;
typedef long glong;
typedef unsigned long gulong;
typedef double gdouble;
typedef char gchar;
typedef unsigned char guchar;
typedef void* gpointer;
typedef const void* gconstpointer;
typedef unsigned long gsize;
typedef long gssize;
typedef unsigned int guint32;
typedef unsigned short guint16;
typedef unsigned char guint8;
typedef int gint32;
typedef short gint16;
typedef signed char gint8;
typedef long long gint64;
typedef unsigned long long guint64;
typedef int GCallback;
typedef void* GObject;
#define G_CALLBACK(f) ((GCallback)(f))
#define G_OBJECT(o) ((GObject)(o))
#define GTK_WINDOW(w) ((GtkWindow*)(w))
#define GTK_BUTTON(b) ((GtkButton*)(b))
#define GTK_LABEL(l) ((GtkLabel*)(l))
#define GTK_TOGGLE_BUTTON(b) ((GtkToggleButton*)(b))
#define GTK_STATUSBAR(s) ((GtkStatusbar*)(s))
#define GTK_TEXT_VIEW(t) ((GtkTextView*)(t))
#define GTK_TEXT_BUFFER(b) ((GtkTextBuffer*)(b))
#define GTK_TREE_VIEW(t) ((GtkTreeView*)(t))
#define GTK_SPIN_BUTTON(s) ((GtkSpinButton*)(s))
#define GTK_COMBO_BOX(c) ((GtkComboBox*)(c))
#define GTK_NOTEBOOK(n) ((GtkNotebook*)(n))
#define GTK_TOOL_BUTTON(t) ((GtkToolButton*)(t))
#define GTK_TOOLBAR(t) ((GtkToolbar*)(t))
#define GTK_TREE_SELECTION(s) ((GtkTreeSelection*)(s))
extern GtkWidget* gtk_button_new(void);
extern GtkWidget* gtk_button_new_with_label(const gchar*);
extern void gtk_button_set_label(GtkButton*, const gchar*);
extern const gchar* gtk_button_get_label(GtkButton*);
extern GtkWidget* gtk_label_new(const gchar*);
extern void gtk_label_set_text(GtkLabel*, const gchar*);
extern void gtk_label_set_markup(GtkLabel*, const gchar*);
extern const gchar* gtk_label_get_text(GtkLabel*);
extern GtkWidget* gtk_entry_new(void);
extern const gchar* gtk_entry_get_text(GtkEntry*);
extern void gtk_entry_set_text(GtkEntry*, const gchar*);
extern void gtk_entry_set_max_length(GtkEntry*, gint);
extern void gtk_widget_show(GtkWidget*);
extern void gtk_widget_show_all(GtkWidget*);
extern void gtk_widget_hide(GtkWidget*);
extern void gtk_widget_set_sensitive(GtkWidget*, gboolean);
extern void gtk_widget_set_visible(GtkWidget*, gboolean);
extern void gtk_widget_destroy(GtkWidget*);
extern void gtk_widget_grab_focus(GtkWidget*);
extern void gtk_widget_queue_draw(GtkWidget*);
extern void gtk_widget_set_tooltip_text(GtkWidget*, const gchar*);
extern void gtk_main(void);
extern void gtk_main_quit(void);
extern int gtk_events_pending(void);
extern int gtk_main_iteration(void);
extern int gtk_main_iteration_do(int);
extern void gtk_init(int*, char***);
extern void gtk_statusbar_push(GtkStatusbar*, guint, const gchar*);
extern void gtk_statusbar_pop(GtkStatusbar*, guint);
extern void gtk_statusbar_remove_all(GtkStatusbar*, guint, guint);
extern gdouble gtk_spin_button_get_value(GtkSpinButton*);
extern int gtk_spin_button_get_value_as_int(GtkSpinButton*);
extern void gtk_spin_button_set_value(GtkSpinButton*, gdouble);
extern void gtk_spin_button_set_range(GtkSpinButton*, gdouble, gdouble);
extern void gtk_spin_button_set_increments(GtkSpinButton*, gdouble, gdouble);
extern gboolean gtk_toggle_button_get_active(GtkToggleButton*);
extern void gtk_toggle_button_set_active(GtkToggleButton*, gboolean);
extern gboolean gtk_toggle_tool_button_get_active(GtkToggleToolButton*);
extern void gtk_toggle_tool_button_set_active(GtkToggleToolButton*, gboolean);
extern gint gtk_combo_box_get_active(GtkComboBox*);
extern void gtk_combo_box_set_active(GtkComboBox*, gint);
extern gchar* gtk_combo_box_text_get_active_text(GtkComboBoxText*);
extern void gtk_combo_box_text_append(GtkComboBoxText*, const gchar*, const gchar*);
extern void gtk_combo_box_text_remove(GtkComboBoxText*, gint);
extern void gtk_combo_box_text_remove_all(GtkComboBoxText*);
extern GtkTextBuffer* gtk_text_view_get_buffer(GtkTextView*);
extern void gtk_text_buffer_set_text(GtkTextBuffer*, const gchar*, gint);
extern gchar* gtk_text_buffer_get_text(GtkTextBuffer*, GtkTextIter*, GtkTextIter*, gboolean);
extern void gtk_text_buffer_get_start_iter(GtkTextBuffer*, GtkTextIter*);
extern void gtk_text_buffer_get_end_iter(GtkTextBuffer*, GtkTextIter*);
extern void gtk_text_buffer_insert(GtkTextBuffer*, GtkTextIter*, const gchar*, gint);
extern void gtk_text_buffer_delete(GtkTextBuffer*, GtkTextIter*, GtkTextIter*);
extern gint gtk_text_buffer_get_line_count(GtkTextBuffer*);
extern void gtk_text_view_scroll_to_iter(GtkTextView*, GtkTextIter*, gdouble, gboolean, gdouble, gdouble);
extern GtkBuilder* gtk_builder_new(void);
extern GtkBuilder* gtk_builder_new_from_file(const gchar*);
extern GtkBuilder* gtk_builder_new_from_resource(const gchar*);
extern GObject* gtk_builder_get_object(GtkBuilder*, const gchar*);
extern void gtk_builder_connect_signals(GtkBuilder*, gpointer);
extern void gtk_window_set_title(GtkWindow*, const gchar*);
extern void gtk_window_set_default_size(GtkWindow*, gint, gint);
extern void gtk_window_set_modal(GtkWindow*, gboolean);
extern void gtk_window_close(GtkWindow*);
extern void gtk_window_present(GtkWindow*);
extern void gtk_window_resize(GtkWindow*, gint, gint);
extern void g_signal_connect(gpointer, const gchar*, GCallback, gpointer);
extern void g_signal_connect_swapped(gpointer, const gchar*, GCallback, gpointer);
extern void g_signal_handlers_disconnect_by_func(gpointer, GCallback, gpointer);
extern void g_object_unref(gpointer);
extern gpointer g_object_get_data(gpointer, const gchar*);
extern void g_object_set_data(gpointer, const gchar*, gpointer);
extern guint g_timeout_add(guint, GCallback, gpointer);
extern guint g_idle_add(GCallback, gpointer);
extern GdkPixbuf* gdk_pixbuf_new_from_resource(const gchar*, gpointer);
extern GtkCssProvider* gtk_css_provider_new(void);
extern void gtk_css_provider_load_from_resource(GtkCssProvider*, const gchar*);
extern void gtk_style_context_add_provider_for_screen(GdkScreen*, gpointer, guint);
extern GdkScreen* gdk_screen_get_default(void);
extern GtkTreeSelection* gtk_tree_view_get_selection(GtkTreeView*);
extern gboolean gtk_tree_selection_get_selected(GtkTreeSelection*, GtkListStore*, GtkTreeIter*);
extern void gtk_tree_selection_select_iter(GtkTreeSelection*, GtkTreeIter*);
extern void gtk_tree_selection_unselect_all(GtkTreeSelection*);
extern void gtk_tree_selection_set_mode(GtkTreeSelection*, gint);
extern void gtk_tree_view_insert_column_with_attributes(GtkTreeView*, gint, const gchar*, gpointer, const gchar*, gint, ...);
extern gint gtk_notebook_get_current_page(GtkNotebook*);
extern void gtk_notebook_set_current_page(GtkNotebook*, gint);
extern gint gtk_notebook_append_page(GtkNotebook*, GtkWidget*, GtkWidget*);
extern void gtk_notebook_remove_page(GtkNotebook*, gint);
extern gdouble gtk_range_get_value(GtkWidget*);
extern void gtk_range_set_value(GtkWidget*, gdouble);
extern void gtk_progress_bar_set_fraction(GtkWidget*, gdouble);
extern void gtk_progress_bar_pulse(GtkWidget*);
extern void gtk_window_set_icon(GtkWindow*, GdkPixbuf*);
#endif
""",
    "glib.h": """#ifndef __GLIB_H__
#define __GLIB_H__
typedef int gboolean;
typedef int gint;
typedef unsigned int guint;
typedef long glong;
typedef unsigned long gulong;
typedef double gdouble;
typedef char gchar;
typedef unsigned char guchar;
typedef void* gpointer;
typedef const void* gconstpointer;
typedef unsigned long gsize;
typedef long gssize;
typedef unsigned int guint32;
typedef unsigned short guint16;
typedef unsigned char guint8;
typedef int gint32;
typedef short gint16;
typedef char gint8;
typedef long long gint64;
typedef unsigned long long guint64;
#endif
""",
}


def main(argv=None) -> int:
    """Generate fake headers. If argv is given, use argv[0] as the
    target directory; otherwise use the module-level ROOT variable."""
    global ROOT
    if argv:
        import sys
        if isinstance(argv, list):
            ROOT = argv[0] if argv else ROOT
        elif isinstance(argv, str):
            ROOT = argv
    os.makedirs(ROOT, exist_ok=True)
    for sub in ("sys", "linux", "asm", "gtk", "glib", "gio"):
        os.makedirs(os.path.join(ROOT, sub), exist_ok=True)
    for name, content in HEADERS.items():
        path = os.path.join(ROOT, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
    print(f"Generated {len(HEADERS)} fake header files in {ROOT}")
    return 0


if __name__ == "__main__":
    import sys
    # Allow: python gen_fake_includes.py [target_dir]
    if len(sys.argv) > 1:
        ROOT = sys.argv[1]
    raise SystemExit(main())


def generate(target_dir: str = None) -> int:
    """Generate fake includes in target_dir. Returns 0 on success.

    If target_dir is None, uses a platform-appropriate default:
      Linux/macOS: /tmp/fake_libc_include
      Windows: %TEMP%\\fake_libc_include
    """
    global ROOT
    import os
    import sys
    if target_dir is None:
        if sys.platform == "win32":
            target_dir = os.path.join(os.environ.get("TEMP", "C:\\Temp"),
                                       "fake_libc_include")
        else:
            target_dir = "/tmp/fake_libc_include"
    ROOT = target_dir
    return main()
