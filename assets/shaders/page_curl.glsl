#version 330
// SAMPLING
uniform vec2 normal, center;
uniform float axis, radius, thickness, front, back, shadow, softness, highlight, depth;
uniform vec3 backColor;
uniform int backMode;
uniform bool antialias, complete;
out vec4 result;
vec4 page(vec2 p) {
    float q = dot(p, normal) - axis;
    if (q < 0.) {
        vec4 unbent = samplePixel(iChannel0, p, 0, false);
        unbent.rgb *= front * (1. - shadow * exp(q / softness));
        return unbent;
    }
    if (q > radius) return vec4(0);
    float theta = asin(clamp(q / radius, 0., 1.));
    vec2 tangent = vec2(-normal.y, normal.x);
    float tx = dot(p - center, tangent) * depth * (1. + cos(theta)) * .15;
    vec2 bp = p + normal * (radius * (3.14159265359 - theta) + thickness - q) + tangent * tx;
    vec4 fold;
    if (all(greaterThanEqual(bp, vec2(0))) && all(lessThanEqual(bp, iResolution.xy - 1.))) {
        fold = samplePixel(iChannel0, bp, 0, false);
        if (backMode == 0) fold = vec4(backColor * fold.a, fold.a);
        else if (backMode == 2) fold = samplePixel(iChannel1, bp, 0, false) * fold.a;
        fold.rgb *= back * (.35 + .65 * sin(theta));
    } else {
        fold = samplePixel(iChannel0, p + normal * (radius * theta - q), 0, false);
        fold.rgb *= front * (.45 + .55 * cos(theta));
    }
    fold.rgb += fold.a * highlight * pow(sin(theta), 8.) * .35;
    return fold;
}
void main() {
    if (complete) { result = vec4(0); return; }
    vec2 p = gl_FragCoord.xy - .5;
    result = antialias ? (page(p+vec2(-.25,-.25)) + page(p+vec2(.25,-.25)) +
                         page(p+vec2(-.25,.25)) + page(p+vec2(.25,.25))) * .25 : page(p);
}
