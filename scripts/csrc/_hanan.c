/*
 * _hanan: C implementation of the Hanan-grid A* search used by
 * pathfinder.astar_route(). It is a line-for-line port of the pure-Python
 * reference (pathfinder.astar_route_py) - same state space
 * (x_index, y_index, incoming_direction), same costs, same heap ordering
 * and floating-point operation order - so both produce identical paths.
 * tests/test_hanan_c.py checks that.
 *
 * Built with the standard library only: scripts/build_ext.py drives the C
 * compiler using the flags CPython was itself built with (sysconfig).
 *
 * astar(xs, ys, blocked, penalty, segs, starts, goals,
 *       buffer, turn_penalty, affinity_penalty, soft_penalty, hard_buffer)
 *
 *   xs, ys       sequences of float, the grid line coordinates
 *   blocked      bytes of len(xs)*len(ys), 1 = inside a node (index i*ny+j)
 *   penalty      sequence of float, same indexing, static grid penalty
 *   segs         sequence of (horizontal: bool, fixed, lo, hi, same_target: bool)
 *                for every segment already in the congestion map
 *   starts/goals sequences of (i, j) grid indices
 *
 * Returns a list of (i, j) from a start to a goal, or None.
 */
#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <math.h>
#include <stdlib.h>
#include <string.h>

typedef struct {
    double f, g;
    int i, j, d;     /* d: 0 = none (start), 1 = H, 2 = V  (None < "H" < "V") */
} HeapItem;

typedef struct {
    HeapItem *items;
    Py_ssize_t len, cap;
} Heap;

typedef struct {
    int horizontal;
    double fixed, lo, hi;
    int same_target;
} Seg;

/* Python tuple ordering on (f, g, (i, j, d)) */
static int item_less(const HeapItem *a, const HeapItem *b)
{
    if (a->f != b->f) return a->f < b->f;
    if (a->g != b->g) return a->g < b->g;
    if (a->i != b->i) return a->i < b->i;
    if (a->j != b->j) return a->j < b->j;
    return a->d < b->d;
}

static int heap_push(Heap *h, HeapItem it)
{
    if (h->len == h->cap) {
        Py_ssize_t ncap = h->cap ? h->cap * 2 : 1024;
        HeapItem *n = realloc(h->items, (size_t)ncap * sizeof(HeapItem));
        if (!n) return -1;
        h->items = n;
        h->cap = ncap;
    }
    /* sift down towards the root, as heapq._siftdown does */
    Py_ssize_t pos = h->len++;
    while (pos > 0) {
        Py_ssize_t parent = (pos - 1) >> 1;
        if (item_less(&it, &h->items[parent])) {
            h->items[pos] = h->items[parent];
            pos = parent;
        } else break;
    }
    h->items[pos] = it;
    return 0;
}

static HeapItem heap_pop(Heap *h)
{
    /* mirrors heapq.heappop: move last to root, then _siftup (bubble the
     * smaller child up to a leaf, then _siftdown) so tie order matches */
    HeapItem last = h->items[--h->len];
    if (h->len == 0) return last;
    HeapItem ret = h->items[0];
    Py_ssize_t end = h->len, pos = 0, child = 1;
    while (child < end) {
        Py_ssize_t right = child + 1;
        if (right < end && !item_less(&h->items[child], &h->items[right]))
            child = right;
        h->items[pos] = h->items[child];
        pos = child;
        child = 2 * pos + 1;
    }
    while (pos > 0) {
        Py_ssize_t parent = (pos - 1) >> 1;
        if (item_less(&last, &h->items[parent])) {
            h->items[pos] = h->items[parent];
            pos = parent;
        } else break;
    }
    h->items[pos] = last;
    return ret;
}

static double *read_doubles(PyObject *seq, Py_ssize_t *n_out)
{
    PyObject *fast = PySequence_Fast(seq, "expected a sequence of floats");
    if (!fast) return NULL;
    Py_ssize_t n = PySequence_Fast_GET_SIZE(fast);
    double *out = malloc((size_t)(n ? n : 1) * sizeof(double));
    if (!out) { Py_DECREF(fast); PyErr_NoMemory(); return NULL; }
    for (Py_ssize_t k = 0; k < n; k++) {
        out[k] = PyFloat_AsDouble(PySequence_Fast_GET_ITEM(fast, k));
        if (out[k] == -1.0 && PyErr_Occurred()) { free(out); Py_DECREF(fast); return NULL; }
    }
    Py_DECREF(fast);
    *n_out = n;
    return out;
}

static int *read_pairs(PyObject *seq, Py_ssize_t *n_out)
{
    PyObject *fast = PySequence_Fast(seq, "expected a sequence of (i, j)");
    if (!fast) return NULL;
    Py_ssize_t n = PySequence_Fast_GET_SIZE(fast);
    int *out = malloc((size_t)(n ? n : 1) * 2 * sizeof(int));
    if (!out) { Py_DECREF(fast); PyErr_NoMemory(); return NULL; }
    for (Py_ssize_t k = 0; k < n; k++) {
        int i, j;
        if (!PyArg_ParseTuple(PySequence_Fast_GET_ITEM(fast, k), "ii", &i, &j)) {
            free(out); Py_DECREF(fast); return NULL;
        }
        out[2 * k] = i;
        out[2 * k + 1] = j;
    }
    Py_DECREF(fast);
    *n_out = n;
    return out;
}

static Seg *read_segs(PyObject *seq, Py_ssize_t *n_out)
{
    PyObject *fast = PySequence_Fast(seq, "expected a sequence of segments");
    if (!fast) return NULL;
    Py_ssize_t n = PySequence_Fast_GET_SIZE(fast);
    Seg *out = malloc((size_t)(n ? n : 1) * sizeof(Seg));
    if (!out) { Py_DECREF(fast); PyErr_NoMemory(); return NULL; }
    for (Py_ssize_t k = 0; k < n; k++) {
        int hz, same;
        if (!PyArg_ParseTuple(PySequence_Fast_GET_ITEM(fast, k), "pdddp",
                              &hz, &out[k].fixed, &out[k].lo, &out[k].hi, &same)) {
            free(out); Py_DECREF(fast); return NULL;
        }
        out[k].horizontal = hz;
        out[k].same_target = same;
    }
    Py_DECREF(fast);
    *n_out = n;
    return out;
}

static PyObject *astar(PyObject *self, PyObject *args)
{
    PyObject *xs_o, *ys_o, *pen_o, *segs_o, *starts_o, *goals_o;
    Py_buffer blocked;
    double buffer, turn_penalty, affinity, soft_penalty;
    int hard_buffer;

    if (!PyArg_ParseTuple(args, "OOy*OOOOddddp", &xs_o, &ys_o, &blocked, &pen_o, &segs_o,
                          &starts_o, &goals_o, &buffer, &turn_penalty, &affinity,
                          &soft_penalty, &hard_buffer))
        return NULL;

    PyObject *result = NULL;
    double *xs = NULL, *ys = NULL, *pen = NULL, *best_g = NULL;
    int *starts = NULL, *goals = NULL, *came_from = NULL;
    unsigned char *goal_mask = NULL;
    Seg *segs = NULL;
    Heap heap = {0};
    Py_ssize_t nx, ny, npen, nseg, nstart, ngoal;

    if (!(xs = read_doubles(xs_o, &nx)) || !(ys = read_doubles(ys_o, &ny)) ||
        !(pen = read_doubles(pen_o, &npen)) || !(segs = read_segs(segs_o, &nseg)) ||
        !(starts = read_pairs(starts_o, &nstart)) || !(goals = read_pairs(goals_o, &ngoal)))
        goto done;
    if (blocked.len != nx * ny || npen != nx * ny) {
        PyErr_SetString(PyExc_ValueError, "blocked/penalty must have len(xs)*len(ys) entries");
        goto done;
    }

    Py_ssize_t ncells = nx * ny, nstates = ncells * 3;
    best_g = malloc((size_t)nstates * sizeof(double));
    came_from = malloc((size_t)nstates * sizeof(int));
    goal_mask = calloc((size_t)ncells, 1);
    if (!best_g || !came_from || !goal_mask) { PyErr_NoMemory(); goto done; }
    for (Py_ssize_t k = 0; k < nstates; k++) { best_g[k] = INFINITY; came_from[k] = -1; }

    for (Py_ssize_t k = 0; k < ngoal; k++) {
        int gi = goals[2 * k], gj = goals[2 * k + 1];
        if (gi < 0 || gi >= nx || gj < 0 || gj >= ny) {
            PyErr_SetString(PyExc_IndexError, "goal index out of range");
            goto done;
        }
        goal_mask[gi * ny + gj] = 1;
    }
    const unsigned char *blk = (const unsigned char *)blocked.buf;

#define STATE(i, j, d) ((((Py_ssize_t)(i)) * ny + (j)) * 3 + (d))
#define HEUR(i, j, out) do {                                                 \
        double _x = xs[i], _y = ys[j], _best = INFINITY;                     \
        for (Py_ssize_t _k = 0; _k < ngoal; _k++) {                          \
            double _v = fabs(_x - xs[goals[2*_k]]) + fabs(_y - ys[goals[2*_k+1]]); \
            if (_v < _best) _best = _v;                                      \
        }                                                                    \
        (out) = _best;                                                       \
    } while (0)

    for (Py_ssize_t k = 0; k < nstart; k++) {
        int si = starts[2 * k], sj = starts[2 * k + 1];
        if (si < 0 || si >= nx || sj < 0 || sj >= ny) {
            PyErr_SetString(PyExc_IndexError, "start index out of range");
            goto done;
        }
        double hval;
        HEUR(si, sj, hval);
        best_g[STATE(si, sj, 0)] = 0.0;
        HeapItem it = {hval, 0.0, si, sj, 0};
        if (heap_push(&heap, it) < 0) { PyErr_NoMemory(); goto done; }
    }

    static const int DI[4] = {1, -1, 0, 0}, DJ[4] = {0, 0, 1, -1};
    while (heap.len) {
        HeapItem cur = heap_pop(&heap);
        int i = cur.i, j = cur.j, d = cur.d;
        double g = cur.g;
        Py_ssize_t st = STATE(i, j, d);
        if (best_g[st] < g) continue;

        if (goal_mask[i * ny + j]) {
            /* reconstruct */
            Py_ssize_t count = 0;
            for (Py_ssize_t s = st; s >= 0; s = came_from[s]) count++;
            result = PyList_New(count);
            if (!result) goto done;
            Py_ssize_t pos = count - 1;
            for (Py_ssize_t s = st; s >= 0; s = came_from[s], pos--) {
                Py_ssize_t cell = s / 3;
                PyObject *pt = Py_BuildValue("(nn)", cell / ny, cell % ny);
                if (!pt) { Py_CLEAR(result); goto done; }
                PyList_SET_ITEM(result, pos, pt);
            }
            goto done;
        }

        for (int k = 0; k < 4; k++) {
            int ni = i + DI[k], nj = j + DJ[k];
            if (ni < 0 || ni >= nx || nj < 0 || nj >= ny) continue;
            if (blk[ni * ny + nj] && !goal_mask[ni * ny + nj]) continue;

            int new_dir = (ni != i) ? 1 : 2;
            double hx = xs[i], hy = ys[j], tx = xs[ni], ty = ys[nj];
            double step_dist = fabs(tx - hx) + fabs(ty - hy);
            double step_cost = step_dist;
            if (d != 0 && new_dir != d) step_cost += turn_penalty;
            step_cost += pen[ni * ny + nj];

            /* candidate segment: horizontal iff the y coordinates match */
            int c_h = fabs(hy - ty) < 1e-6;
            double c_fixed = c_h ? hy : hx;
            double c_lo = c_h ? fmin(hx, tx) : fmin(hy, ty);
            double c_hi = c_h ? fmax(hx, tx) : fmax(hy, ty);

            int conflict = 0;
            for (Py_ssize_t s = 0; s < nseg; s++) {
                const Seg *sg = &segs[s];
                if (sg->horizontal != c_h) continue;
                if (fabs(c_fixed - sg->fixed) >= buffer - 1e-6) continue;
                if (c_lo < sg->hi && sg->lo < c_hi) { conflict = 1; break; }
            }
            if (conflict) {
                if (hard_buffer) continue;
                step_cost += soft_penalty;
            } else if (nseg) {
                int adjacent = 0;
                for (Py_ssize_t s = 0; s < nseg; s++) {
                    const Seg *sg = &segs[s];
                    if (sg->same_target && sg->horizontal == c_h &&
                        fabs(fabs(c_fixed - sg->fixed) - buffer) < 1e-4 &&
                        c_lo < sg->hi && sg->lo < c_hi) { adjacent = 1; break; }
                }
                if (!adjacent) step_cost += step_dist * affinity;
            }

            Py_ssize_t ns = STATE(ni, nj, new_dir);
            double tentative = g + step_cost;
            if (tentative < best_g[ns]) {
                best_g[ns] = tentative;
                came_from[ns] = (int)st;
                double hval;
                HEUR(ni, nj, hval);
                HeapItem it = {tentative + hval, tentative, ni, nj, new_dir};
                if (heap_push(&heap, it) < 0) { PyErr_NoMemory(); goto done; }
            }
        }
    }
    result = Py_None;
    Py_INCREF(result);

done:
    PyBuffer_Release(&blocked);
    free(xs); free(ys); free(pen); free(segs); free(starts); free(goals);
    free(best_g); free(came_from); free(goal_mask); free(heap.items);
    return result;
}

static PyMethodDef methods[] = {
    {"astar", astar, METH_VARARGS, "Hanan-grid A* search (see module docstring)."},
    {NULL, NULL, 0, NULL},
};

static struct PyModuleDef module = {
    PyModuleDef_HEAD_INIT, "_hanan",
    "C implementation of pathfinder's Hanan-grid A* search.", -1, methods,
};

PyMODINIT_FUNC PyInit__hanan(void)
{
    return PyModule_Create(&module);
}
