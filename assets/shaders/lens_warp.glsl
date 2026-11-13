#version 330
// SAMPLING
uniform vec2 center;
uniform float radius, aspect, amount, chromatic;
uniform int edge;
uniform bool nearest;
out vec4 result;
void main() {
    vec2 p = gl_FragCoord.xy - .5;
    vec2 delta = p - center;
    float d2 = dot(delta / vec2(radius * aspect, radius), delta / vec2(radius * aspect, radius));
    float f = pow(max(0., 1. - d2), 2.);
    float scale = 1. - amount * f;
    vec4 r = samplePixel(iChannel0, p + delta * (scale - 1. - chromatic * f), edge, nearest);
    vec4 g = samplePixel(iChannel0, p + delta * (scale - 1.), edge, nearest);
    vec4 b = samplePixel(iChannel0, p + delta * (scale - 1. + chromatic * f), edge, nearest);
    result = vec4(r.r, g.g, b.b, max(r.a, max(g.a, b.a)));
}
