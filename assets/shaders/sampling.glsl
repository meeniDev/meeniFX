uniform sampler2D iChannel0;
uniform sampler2D iChannel1;
uniform vec3 iResolution;
uniform float iTime;
uniform int iFrame;
vec4 fetchPixel(sampler2D source, ivec2 p, int edge) {
    ivec2 size = textureSize(source, 0);
    if (edge == 0 && (any(lessThan(p, ivec2(0))) || any(greaterThanEqual(p, size)))) return vec4(0);
    if (edge == 2) {
        ivec2 period = max(ivec2(1), 2 * (size - 1));
        p = ((p % period) + period) % period;
        p = min(p, period - p);
    }
    return texelFetch(source, clamp(p, ivec2(0), size - 1), 0);
}
vec4 samplePixel(sampler2D source, vec2 p, int edge, bool nearest) {
    if (nearest) return fetchPixel(source, ivec2(floor(p + .5)), edge);
    ivec2 q = ivec2(floor(p));
    vec2 f = fract(p);
    return mix(mix(fetchPixel(source,q,edge), fetchPixel(source,q+ivec2(1,0),edge), f.x),
               mix(fetchPixel(source,q+ivec2(0,1),edge), fetchPixel(source,q+ivec2(1,1),edge), f.x), f.y);
}
